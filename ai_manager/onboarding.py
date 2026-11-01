"""Add User: onboarding records (name, email, state, receipt) and their route to the privileged host helper.

One record is one state: pending -> enrolling -> provisioning -> done | failed (Retry: failed -> provisioning). The web app
never runs as root. Planning and applying are spooled to `<data>/onboarding/requests/<id>.json`; the user path unit
installed by scripts/install_onboarding.py starts the root-owned copy of ai_manager/provision.py through its sudoers rule,
which writes `<data>/onboarding/results/<id>.json`; `refresh()` folds results into the record. Runbook: docs/ONBOARDING.md.
"""
import importlib
import json
import os
import re
import secrets
import sys
import tempfile
import time
from pathlib import Path

from . import enrollment, tailnet_policy
from .config import CODE_ROOT
from .provision import DEFAULT_CONFIG, HOST_CONFIG, ProvisionError, http_json, load_config, validate_request

STATES = ("pending", "enrolling", "provisioning", "done", "failed")
PLAN_WAIT, APPLY_WAIT = 300, 3 * 3600
INSTALL_HINT = ("The host helper is not installed. Once, as root: sudo python3 scripts/install_onboarding.py install; "
                "then as the manager's user: python3 scripts/install_onboarding.py activate (docs/ONBOARDING.md).")


def instance_url(steps):
    """The tailnet URL the `tailnet serve` step reports (empty before the plan has answered, or with a Cloudflare domain)."""
    for title, _status, detail in steps:
        if title == "tailnet serve" and (found := re.search(r"https://[A-Za-z0-9.-]+:\d+", detail or "")):
            return found.group(0)
    return ""


def instance_port(steps):
    url = instance_url(steps)
    return int(url.rsplit(":", 1)[1]) if url else None


class Unknown(Exception):
    """No such onboarding record."""


class NeedsAuthKey(ValueError):
    """No Tailscale API key is configured: the owner pastes a single-use auth key instead."""


def deploy_registry():
    scripts = str(CODE_ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.append(scripts)
    return importlib.import_module("install_deploy")


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".req-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Onboarding:
    def __init__(self, db, settings, config_path=DEFAULT_CONFIG, http=http_json, host_config=HOST_CONFIG, now=time.time):
        self.db, self.settings, self.config_path, self.http, self.host_config, self.now = db, settings, config_path, http, host_config, now
        self.root = settings.data_dir / "onboarding"
        self._manager_ip, self._manager_ip_tried = "", None

    # -- records -------------------------------------------------------------------------------------------------
    def config(self):
        return load_config(self.config_path, os.environ)

    def public(self, row):
        steps = json.loads(row["steps"])
        return {"id": row["id"], "name": row["name"], "email": row["email"], "windows_folder": row["windows_folder"],
                "domain": row["domain"], "memory_max": row["memory_max"], "state": row["state"],
                "auto_apply": bool(row["auto_apply"]), "device_id": row["device_id"], "pc_hostname": row["pc_hostname"],
                "tailscale_ip": row["tailscale_ip"], "busy": bool(row["pending_action"]), "action": row["pending_action"],
                "token_active": bool(row["token_hash"]) and (row["token_expires_at"] or 0) > self.now(),
                "token_expires_at": row["token_expires_at"] if row["token_hash"] else None,
                "steps": [{"title": t, "status": s, "detail": d} for t, s, d in steps], "error": row["error"],
                "has_login": bool(row["login_secret"]), "created_at": row["created_at"], "updated_at": row["updated_at"],
                "hostname": f"{row['name']}.{row['domain']}" if row["domain"] else "",
                "url": instance_url(steps), "tag": enrollment.user_tag(row["name"]),
                "policy": self.policy(row["name"], instance_port(steps))}

    def manager_ip(self):
        """The manager's tailnet IP for the snippet (cached once known; a failed lookup is retried at most every 30 seconds)."""
        if not self._manager_ip and (self._manager_ip_tried is None or self.now() - self._manager_ip_tried > 30):
            self._manager_ip_tried = self.now()
            self._manager_ip = enrollment.manager_tailnet_ip(self.config())
        return self._manager_ip

    def policy(self, name, port):
        """The tailnet policy snippet for this user's tag (to be applied by the owner; the app never edits the policy),
        or None while the instance port is not allocated yet (Re-check plan)."""
        return tailnet_policy.snippet([(name, port)], self.manager_ip()) if port else None

    def row(self, identifier):
        row = self.db.one("SELECT * FROM onboarding WHERE id=?", (identifier,))
        if not row:
            raise Unknown("Unknown onboarding record")
        return row

    def update(self, identifier, **fields):
        names = ", ".join(f"{key}=?" for key in fields)
        self.db.run(f"UPDATE onboarding SET {names}, updated_at=? WHERE id=?", (*fields.values(), self.now(), identifier))

    def list(self):
        self.refresh()
        return [self.public(r) for r in self.db.all("SELECT * FROM onboarding ORDER BY created_at DESC")]

    def get(self, identifier):
        self.refresh()
        return self.public(self.row(identifier))

    def create(self, fields, auto_apply=False):
        if not self.installed():
            raise ValueError(INSTALL_HINT)
        try:
            clean = validate_request(fields)  # no domain means tailnet-only; a domain is never filled in from the config
        except ProvisionError as error:
            raise ValueError(str(error)) from error
        if self.db.one("SELECT 1 FROM onboarding WHERE name=?", (clean["name"],)):
            raise ValueError(f"{clean['name']} already has an onboarding record; open it instead")
        identifier, now = secrets.token_hex(16), self.now()
        self.db.run("INSERT INTO onboarding(id,name,email,windows_folder,domain,memory_max,device_id,auto_apply,"
                        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (identifier, clean["name"], clean["email"], clean["windows_folder"], clean["domain"],
                         clean["memory_max"], clean["device_id"], int(auto_apply), now, now))
        self.request(identifier, "plan")
        return self.get(identifier)

    def settings_update(self, identifier, device_id=None, auto_apply=None):
        row = self.row(identifier)
        if row["state"] in ("provisioning", "done"):
            raise ValueError("This record can no longer be changed")
        changes = {}
        if auto_apply is not None:
            changes["auto_apply"] = int(auto_apply)
        if device_id is not None:
            changes["device_id"] = validate_request({**dict(row), "device_id": device_id})["device_id"]
        if changes:
            self.update(identifier, **changes)
        return self.get(identifier)

    # -- the spool -----------------------------------------------------------------------------------------------
    def installed(self):
        return (self.root / "installed.json").is_file()

    def request(self, identifier, action):
        """Hand the validated request to the host helper. Planning changes nothing anywhere."""
        if not self.installed():
            raise ValueError(INSTALL_HINT)
        self.refresh()  # an answer that has already arrived must not look like a running request
        row = self.row(identifier)
        if row["pending_action"]:
            raise ValueError("A request for this user is already running")
        private_json(self.root / "requests" / f"{identifier}.json", {
            "id": identifier, "action": action, "name": row["name"], "email": row["email"],
            "windows_folder": row["windows_folder"], "domain": row["domain"], "memory_max": row["memory_max"],
            "device_id": row["device_id"], "tailscale_ip": row["tailscale_ip"] or "", "port": row["port"] or 0})
        (self.root / "results").mkdir(parents=True, exist_ok=True, mode=0o700)
        (self.root / "results" / f"{identifier}.json").unlink(missing_ok=True)
        self.update(identifier, pending_action=action, requested_at=self.now())

    def preview(self, identifier):
        self.request(identifier, "plan")
        return self.get(identifier)

    def apply(self, identifier):
        self.refresh()
        row = self.row(identifier)
        if row["state"] not in ("pending", "enrolling", "failed"):
            raise ValueError("This user is already being provisioned or is done")
        if not row["device_id"]:
            raise ValueError("Waiting for the PC: run the enrollment command on it first (or enter its Syncthing device ID)")
        self.request(identifier, "apply")
        # The enrollment token has done its job; nothing can change the device ID from here on.
        self.update(identifier, state="provisioning", error="", token_hash=None, token_expires_at=None)
        return self.get(identifier)

    def refresh(self):
        for row in self.db.all("SELECT * FROM onboarding WHERE pending_action IS NOT NULL"):
            path = self.root / "results" / f"{row['id']}.json"
            try:
                result = json.loads(path.read_text())
            except (OSError, ValueError):
                limit = APPLY_WAIT if row["pending_action"] == "apply" else PLAN_WAIT
                if self.now() - (row["requested_at"] or 0) > limit:
                    self.finish(row, {"receipt": [["host helper", "failed", "no answer from the host helper; is the path unit active? "
                                                   "(systemctl --user status linko-bot-manager-onboarding.path)"]]})
                continue
            if result.get("finished"):
                self.finish(row, result)
                path.unlink(missing_ok=True)
            else:
                self.update(row["id"], steps=json.dumps(result.get("receipt", [])))

    def finish(self, row, result):
        receipt = [list(r) for r in result.get("receipt", [])]
        problem = next((f"{t}: {d}" for t, s, d in receipt if s in ("failed", "refused")), "")
        fields = {"pending_action": None}
        if result.get("port"):
            fields["port"] = int(result["port"])  # the preview's port is the one apply uses
        if row["pending_action"] == "apply":
            login = result.get("login")
            if login:
                fields["login_secret"] = json.dumps(login)
            if not problem:
                try:
                    receipt.append(list(self.register(row, result)))
                except (ProvisionError, ValueError, OSError) as error:
                    problem = f"deploy registration: {error}"
                    receipt.append(["deploy registration", "failed", str(error)])
            fields.update(state="failed" if problem else "done", error=problem)
        elif problem:
            fields["error"] = problem
        else:
            fields["error"] = ""
        self.update(row["id"], steps=json.dumps(receipt), **fields)

    def register(self, row, result):
        """Register the new instance for coordinated deploys (scripts/install_deploy.py --instance logic)."""
        name = row["name"]
        entry = {"name": name, "service": result["unit"], "data": result["data"], "url": f"http://127.0.0.1:{result['port']}",
                 "service_cmds": {action: self.service_command(action, name) for action in ("stop", "start")}}
        title = "deploy registration"
        if not self.settings.data_dir.joinpath("deploy", "installed.json").is_file():
            return title, "skipped", "deploys are not installed on this manager; run scripts/install_deploy.py --instance " + name + " later"
        changed = deploy_registry().register_instances(self.settings.data_dir, [entry])
        return title, "created" if changed else "skipped", f"{name} restarts and is health-checked with every deploy"

    def service_command(self, action, name):
        host = json.loads(Path(self.host_config).read_text())
        return ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", host["launcher"], "--data", host["data"], "service", action, name]

    # -- the PC reports in ---------------------------------------------------------------------------------------
    def enrollment_command(self, identifier, auth_key=None, run=None):
        row = self.row(identifier)
        if row["state"] not in ("pending", "enrolling"):
            raise ValueError("Enrollment is only possible before provisioning starts")
        config = self.config()
        if auth_key:
            if not enrollment.AUTH_KEY_RE.match(auth_key.strip()):
                raise ValueError("That does not look like a Tailscale auth key (tskey-auth-...)")
            auth_key = auth_key.strip()
        elif config.get("TAILSCALE_API_KEY") and config.get("TAILSCALE_TAILNET"):
            auth_key = enrollment.mint_auth_key(config, row["name"], self.http)
        else:
            raise NeedsAuthKey("No Tailscale API key is configured: paste a single-use auth key instead")
        token, hashed, expires = enrollment.new_token(self.now())
        command = enrollment.build_command(
            auth_key=auth_key, public_key=enrollment.management_public_key(config, Path.home()),
            windows_folder=row["windows_folder"], url=enrollment.manager_url(config, run, self.settings.port),
            manager_device=enrollment.syncthing_device_id(config, self.http), token=token, folder_id=f"linko-{row['name']}")
        # A new command replaces any earlier token, so only the line on screen can enroll.
        self.update(identifier, state="enrolling", token_hash=hashed, token_expires_at=expires)
        return {"command": command, "expires_at": expires}

    def report(self, token, hostname, tailscale_ip, device_id):
        """The enrolled PC's one report. Wrong, expired and reused tokens are indistinguishable (PermissionError)."""
        hashed = enrollment.digest(token)
        with self.db.transaction() as conn:
            # Finding and consuming the token is one atomic step (BEGIN IMMEDIATE): it works exactly once.
            row = conn.execute("SELECT id,auto_apply FROM onboarding WHERE token_hash=? AND token_expires_at>? "
                               "AND state='enrolling'", (hashed, self.now())).fetchone()
            if row:
                conn.execute("UPDATE onboarding SET token_hash=NULL, token_expires_at=NULL, device_id=?, pc_hostname=?, "
                             "tailscale_ip=?, updated_at=? WHERE id=?", (device_id, hostname, tailscale_ip, self.now(), row["id"]))
        if not row:
            raise PermissionError("Invalid or expired enrollment token")
        if row["auto_apply"]:
            try:
                self.apply(row["id"])
            except ValueError as error:
                self.update(row["id"], error=str(error))
        return {"accepted": True}

    def reveal_login(self, identifier):
        """The generated first login, exactly once. The host helper already deleted initial-login.txt when it read it."""
        with self.db.transaction() as conn:
            row = conn.execute("SELECT login_secret FROM onboarding WHERE id=?", (identifier,)).fetchone()
            if not row:
                raise Unknown("Unknown onboarding record")
            if not row["login_secret"]:
                raise ValueError("The initial login was already shown, or this user already had a login")
            conn.execute("UPDATE onboarding SET login_secret=NULL, updated_at=? WHERE id=?", (self.now(), identifier))
        return json.loads(row["login_secret"])

"""Onboard one restricted user/instance: `python -m ai_manager.provision add-user ...` (dry run unless --apply).

Every step is check-then-reconcile: `check()` only reads and says exists / todo / refuse, `apply()` changes things only
when the check said todo. All checks run before any change, so a refusal (a hostname that already routes elsewhere, a
Syncthing folder path in use) leaves the host untouched. API tokens come from the environment or an ignored config file
outside git (default ~/.config/linko-onboarding.env); nothing secret is written to generated files or the receipt.
Runbook: docs/ONBOARDING.md.

Access is tailnet-only by default: with no domain the instance is published with `tailscale serve` on its own port and no
Cloudflare step exists. Cloudflare (tunnel route, DNS, Access) is an opt-in appendix that needs an explicit domain and a
tunnel dedicated to onboarding (see `guard_tunnel`); the production Messenger tunnel is refused by name.

The web app (never root) reaches the privileged part through a spool: it writes `<data>/onboarding/requests/<id>.json`,
a user path unit starts `sudo -n python3 -I <root-owned copy of this file> --data <data> tick`, and `tick` validates the
request again, runs the same `provision()` as root and writes `<data>/onboarding/results/<id>.json`. This module is
therefore standard library only at import time (the root copy runs outside the repository).
"""
import argparse
import fcntl
import ipaddress
import json
import os
import pwd
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

EXISTS, TODO, REFUSE = "exists", "todo", "refuse"
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
EMAIL_RE = re.compile(r"^[^@\s,\"']+@[^@\s,\"']+\.[^@\s,\"']+$")
ID_RE = re.compile(r"^[0-9a-f]{32}$")
DEVICE_RE = re.compile(r"^[A-Z0-9]{7}(-[A-Z0-9]{7}){7}$")
DOMAIN_RE = re.compile(r"^(?=.{4,200}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,}$")
MEMORY_RE = re.compile(r"^[1-9][0-9]{0,5}[MG]$")
FOLDER_RE = re.compile(r"^[^\x00-\x1f\x7f'\"`\u2018\u2019\u201c\u201d$]{1,260}$")
FIRST_PORT, LAST_PORT = 20000, 29999  # instance ports are picked at random from this high range
MANAGER_PORT = 8787  # the manager's own port: never given to an instance
PORT_ATTEMPTS = 200
DEFAULT_CONFIG = Path("~/.config/linko-onboarding.env")
HOST_CONFIG = Path("/etc/linko/onboarding.json")
CLIENTS_KEY = "AI_MANAGER_ALLOWED_CLIENTS"
TAILNET = ipaddress.ip_network("100.64.0.0/10")
CF_API = "https://api.cloudflare.com/client/v4"
PRODUCTION_TUNNEL = "linko-messenger-webhook"  # carries production Messenger traffic: never touched by onboarding
DEDICATED_PREFIX = "linko-onboarding"  # a Cloudflare tunnel may only be edited when its name starts with this
CREDENTIAL_NOTE = "AI provider login (Codex/Claude) is a manual step done as the new user; nothing is copied from the owner."


class ProvisionError(Exception):
    pass


def validate_request(fields):
    """The one validation of what an owner may ask for (CLI, web app and the root `tick` all use it)."""
    clean = {key: str(fields.get(key) or "").strip() for key in ("name", "email", "windows_folder", "domain", "memory_max", "device_id")}
    clean["memory_max"] = clean["memory_max"] or "1G"
    if not NAME_RE.match(clean["name"]):
        raise ProvisionError("name: lowercase letters, digits and dashes, starting with a letter (2-31 characters)")
    if not EMAIL_RE.match(clean["email"]) or len(clean["email"]) > 200:
        raise ProvisionError("email is not a valid address")
    if not FOLDER_RE.match(clean["windows_folder"]):
        raise ProvisionError("windows folder: 1-260 characters, no quotes, backticks, $ or control characters")
    if clean["domain"] and not DOMAIN_RE.match(clean["domain"]):
        raise ProvisionError("domain is not a valid DNS name")
    if not MEMORY_RE.match(clean["memory_max"]):
        raise ProvisionError("memory limit looks like 512M or 2G")
    if clean["device_id"] and not DEVICE_RE.match(clean["device_id"]):
        raise ProvisionError("Syncthing device ID looks like AAAAAAA-BBBBBBB-... (8 groups of 7)")
    return clean


def in_tailnet(address):
    """True for a Tailscale IPv4 address (100.64.0.0/10)."""
    try:
        return ipaddress.ip_address(address) in TAILNET
    except ValueError:
        return False


def load_config(path, environ):
    """Env vars win over the ignored KEY=VALUE config file (never inside a git work tree by convention)."""
    values = {}
    path = Path(path).expanduser()
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip("\"'")
    values.update({k: v for k, v in environ.items() if k.startswith(("CLOUDFLARE_", "SYNCTHING_", "TAILSCALE_", "LINKO_")) and v})
    return values


def run_command(argv, **kwargs):
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=120, **kwargs)
    except (OSError, subprocess.SubprocessError) as error:
        return 127, f"{argv[0]}: {error}"
    return done.returncode, (done.stdout + done.stderr).strip()


def http_json(method, url, headers=None, body=None):
    """(status, parsed JSON or None). Network errors raise ProvisionError."""
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, None
    except OSError as error:
        raise ProvisionError(f"cannot reach {url.split('?')[0]}: {error}") from error


def port_is_free(port):
    """Nothing listens on the port on loopback or on any address (a bind on either would collide)."""
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return False
    for address in ("127.0.0.1", "0.0.0.0"):
        with socket.socket() as probe:
            try:
                probe.bind((address, port))
            except OSError:
                return False
    return True


def random_port():
    return FIRST_PORT + secrets.randbelow(LAST_PORT - FIRST_PORT + 1)


HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")


def tailnet_host(config, run=run_command):
    """The tailnet DNS name of this machine (no scheme, no port)."""
    url = (config.get("LINKO_MANAGER_URL") or "").strip()
    host = (urllib.parse.urlsplit(url).hostname or "") if url else ""
    if not host:
        code, output = run(["tailscale", "status", "--json"])
        try:
            host = json.loads(output)["Self"]["DNSName"].rstrip(".") if code == 0 else ""
        except (ValueError, KeyError, TypeError, AttributeError):
            host = ""
    if not host or not HOST_RE.match(host):
        raise ProvisionError("cannot determine the tailnet hostname: set LINKO_MANAGER_URL (https://<host>.<tailnet>.ts.net) "
                             "in the onboarding config, or log this machine in to Tailscale")
    return host.lower()


def serve_status(run):
    """`tailscale serve status --json` as a dict; ProvisionError when Tailscale cannot be asked."""
    code, output = run(["tailscale", "serve", "status", "--json"])
    if code:
        raise ProvisionError(f"tailscale serve status failed: {output[-200:]}")
    try:
        status = json.loads(output or "{}")
    except ValueError:
        raise ProvisionError("tailscale serve status --json did not return JSON") from None
    if not isinstance(status, dict):
        raise ProvisionError("tailscale serve status --json did not return an object")
    return status


def served_port(key, port):
    return key.rsplit(":", 1)[-1] == str(port) if ":" in key else False


def serve_ports(status):
    """Every port `tailscale serve` already uses (HTTPS, TCP forward, Web and Funnel keys), as ints."""
    found = set()
    for key in (status.get("TCP") or {}):
        if str(key).isdigit():
            found.add(int(key))
    for section in ("Web", "AllowFunnel"):
        for key in (status.get(section) or {}):
            tail = str(key).rsplit(":", 1)[-1] if ":" in str(key) else ""
            if tail.isdigit():
                found.add(int(tail))
    return found


def local_target(target, port):
    """True for http://127.0.0.1:<port> (or localhost) with no path."""
    parts = urllib.parse.urlsplit(str(target))
    return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost") and parts.port == port and parts.path in ("", "/")


def funnel_ports(status, port):
    """Keys of AllowFunnel that enable Funnel for this port (any host)."""
    return [key for key, on in (status.get("AllowFunnel") or {}).items() if on and served_port(key, port)]


def serve_state(status, port):
    """('absent'|'same'|'other'|'funnel', detail) for one HTTPS port in a parsed serve status."""
    if funnel_ports(status, port):
        return "funnel", f"Funnel (public internet) is on for port {port}: {', '.join(funnel_ports(status, port))}"
    web = {k: v for k, v in (status.get("Web") or {}).items() if served_port(k, port)}
    tcp = (status.get("TCP") or {}).get(str(port))
    if not web and not tcp:
        return "absent", ""
    for key, entry in web.items():
        handlers = (entry or {}).get("Handlers") or {}
        proxy = (handlers.get("/") or {}).get("Proxy")
        if set(handlers) != {"/"} or not proxy or not local_target(proxy, port):
            return "other", f"{key} is already served to {', '.join(f'{p} -> ' + str(h.get('Proxy') or h.get('Path') or h.get('Text') or '?') for p, h in handlers.items()) or 'something else'}"
    if not web:
        return "other", f"port {port} is already forwarded by Tailscale ({json.dumps(tcp)[:80]})"
    return "same", ""


@dataclass
class Plan:
    name: str
    email: str
    windows_folder: str
    domain: str  # "" = tailnet-only (no Cloudflare)
    repo: Path
    python: str
    home_base: Path
    config: dict
    apply: bool
    memory_max: str = "1G"
    syncthing_device: str = ""
    syncthing_url: str = "http://127.0.0.1:8384"
    skip_syncthing: bool = False
    systemd_dir: Path = Path("/etc/systemd/system")
    run: object = run_command
    http: object = http_json
    port_free: object = port_is_free
    pick_port: object = random_port
    port: int = 0  # the port saved on the onboarding record (request "port"): kept when still free, so preview == apply
    onboarding_hostnames: frozenset = frozenset()  # hostnames onboarding created (DB records + allow-list): the only ones a tunnel may hold
    service_user: str = ""  # the Linux user the manager runs as: gets scoped access to the new instance's data
    syncthing_user: str = ""  # the Linux user the Syncthing daemon runs as: gets scoped access to the new folder
    client_ip: str = ""  # the enrolled PC's Tailscale IP (its report); "" until it has reported in

    @property
    def home(self):
        return self.home_base / self.name

    @property
    def project(self):
        return self.home / "Documents" / self.name

    @property
    def data(self):
        return self.home / ".local/share" / f"linko-bot-manager-{self.name}"

    @property
    def env_file(self):
        return self.home / ".config" / f"linko-bot-manager-{self.name}.env"

    @property
    def unit_name(self):
        return f"linko-bot-manager-{self.name}.service"

    @property
    def hostname(self):
        """The Cloudflare hostname; empty in tailnet-only mode."""
        return f"{self.name}.{self.domain}" if self.domain else ""

    def tailnet_host(self):
        """This machine's tailnet DNS name: host of LINKO_MANAGER_URL, else `tailscale status --json` Self.DNSName."""
        if not hasattr(self, "_tailnet_host"):
            self._tailnet_host = tailnet_host(self.config, self.run)
        return self._tailnet_host

    @property
    def url(self):
        """Where the person reaches the instance."""
        if self.domain:
            return f"https://{self.hostname}"
        return f"https://{self.tailnet_host()}:{self.port}"

    @property
    def folder_id(self):
        return f"linko-{self.name}"


class Step:
    title = ""
    state = ""
    detail = ""

    def __init__(self, plan):
        self.plan = plan

    def check(self):
        raise NotImplementedError

    def apply(self):
        raise NotImplementedError


def sh(plan, argv, **kwargs):
    code, output = plan.run(argv, **kwargs)
    if code:
        raise ProvisionError(f"{' '.join(argv[:3])} failed: {output[-300:]}")
    return output


class LinuxUser(Step):
    title = "linux user"

    def check(self):
        code, uid = self.plan.run(["id", "-u", self.plan.name])
        if code == 0:
            if uid == "0":
                return REFUSE, "refusing to use root"
            return EXISTS, f"user {self.plan.name} present"
        return TODO, f"useradd {self.plan.name} (unprivileged, no sudo, no password)"

    def apply(self):
        sh(self.plan, ["useradd", "--create-home", "--home-dir", str(self.plan.home), "--shell", "/bin/bash", self.plan.name])
        return "created"


class Directories(Step):
    title = "project and data dirs"

    def _wanted(self):
        p = self.plan
        return [p.project, p.data, p.env_file.parent]

    def check(self):
        missing = [d for d in self._wanted() if not d.is_dir()]
        return (TODO, "create " + ", ".join(map(str, missing))) if missing else (EXISTS, "all present")

    def apply(self):
        for folder in self._wanted():
            folder.mkdir(parents=True, exist_ok=True)
        sh(self.plan, ["chown", "-R", f"{self.plan.name}:{self.plan.name}", str(self.plan.home)])
        self.plan.data.chmod(0o700)
        return "created"


class GitInit(Step):
    title = "git repository"

    def check(self):
        return (EXISTS, "already a git repository") if (self.plan.project / ".git").exists() else (TODO, f"git init {self.plan.project}")

    def apply(self):
        sh(self.plan, ["runuser", "-u", self.plan.name, "--", "git", "init", str(self.plan.project)])
        return "created"


class Port(Step):
    title = "local port"

    def check(self):
        p = self.plan
        if p.env_file.is_file():
            match = re.search(r"^AI_MANAGER_PORT=(\d+)$", p.env_file.read_text(), re.M)
            if match:
                p.port = int(match.group(1))
                return EXISTS, f"port {p.port} from existing env file"
        taken = {MANAGER_PORT}
        for env in list(p.home_base.glob("*/.config/linko-bot-manager*.env")) + list(Path.home().glob(".config/linko-bot-manager*.env")):
            taken.update(int(m) for m in re.findall(r"^AI_MANAGER_PORT=(\d+)$", env.read_text(errors="ignore"), re.M))
        try:
            taken |= serve_ports(serve_status(p.run))
        except ProvisionError:
            if not p.domain:  # tailnet-only instances are published by `tailscale serve`: it must be answerable
                raise
        saved = p.port if FIRST_PORT <= p.port <= LAST_PORT else 0
        for attempt in range(PORT_ATTEMPTS):
            port = saved if attempt == 0 and saved else p.pick_port()
            if FIRST_PORT <= port <= LAST_PORT and port not in taken and p.port_free(port):
                p.port = port
                return TODO, f"allocate 127.0.0.1:{port}"
        p.port = 0
        return REFUSE, f"no free port found in {FIRST_PORT}-{LAST_PORT} after {PORT_ATTEMPTS} attempts"

    def apply(self):
        return "allocated"  # persisted by the env file step


class EnvFile(Step):
    title = "instance env file"

    def hosts(self):
        """Hostnames the instance accepts: with a domain `<name>.<domain>`, tailnet-only the machine's tailnet name."""
        p = self.plan
        return ["localhost", "127.0.0.1", p.hostname or p.tailnet_host()]

    def content(self):
        p = self.plan
        return (
            "AI_MANAGER_RESTRICTED=1\n"
            f"AI_MANAGER_PROJECT={p.project}\n"
            f"AI_MANAGER_DATA={p.data}\n"
            f"AI_MANAGER_PORT={p.port}\n"
            f"AI_MANAGER_ALLOWED_HOSTS={','.join(self.hosts())}\n"
            "AI_MANAGER_SECURE_COOKIE=1\n"
            + (f"{CLIENTS_KEY}={p.client_ip}\n" if p.client_ip else "")
        )

    def check(self):
        path = self.plan.env_file
        if path.is_file():
            if "AI_MANAGER_RESTRICTED=1" not in path.read_text().splitlines():
                return REFUSE, f"{path} exists but is not restricted; not overwriting"
            return EXISTS, f"{path} present"
        return TODO, f"write {path} (restricted, no secrets)"

    def apply(self):
        path = self.plan.env_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.content())
        path.chmod(0o600)
        sh(self.plan, ["chown", f"{self.plan.name}:{self.plan.name}", str(path)])
        return "created"


class AllowedClients(Step):
    """App-level isolation: the instance answers only the enrolled PC's Tailscale IP (and loopback health checks).
    Idempotent; a different existing value is refused, never overwritten. Network-level layer: ai_manager/tailnet_policy.py."""
    title = "client allow-list"

    def current(self):
        path = self.plan.env_file
        if not path.is_file():
            return None
        found = re.findall(rf"^{CLIENTS_KEY}=(.*)$", path.read_text(), re.M)
        return found[-1].strip() if found else ""

    def check(self):
        p = self.plan
        if not in_tailnet(p.client_ip):
            raise ProvisionError(f"{p.client_ip} is not a Tailscale address (100.64.0.0/10)")
        current = self.current()
        if current is None:
            return EXISTS, f"{CLIENTS_KEY}={p.client_ip} is written with the new env file"
        if current == p.client_ip:
            return EXISTS, f"only {p.client_ip} (and loopback) may use this instance"
        if current:
            return REFUSE, f"{p.env_file} already has {CLIENTS_KEY}={current}, not {p.client_ip}; not overwriting"
        return TODO, f"add {CLIENTS_KEY}={p.client_ip} to {p.env_file} and restart the instance"

    def apply(self):
        p = self.plan
        text = p.env_file.read_text()
        with p.env_file.open("a") as stream:  # appending keeps the file's owner and mode
            stream.write(("" if text.endswith("\n") else "\n") + f"{CLIENTS_KEY}={p.client_ip}\n")
        sh(p, ["systemctl", "try-restart", p.unit_name])  # a no-op when the instance is not running yet
        return "created"


class SystemdUnit(Step):
    title = "systemd unit"

    def content(self):
        p = self.plan
        return (
            f"[Unit]\nDescription=Linko Bot Boss restricted instance {p.name}\nAfter=network-online.target\nWants=network-online.target\n\n"
            f"[Service]\nType=simple\nUser={p.name}\nWorkingDirectory={p.repo}\n"
            f"ExecStart={p.python} -m ai_manager.cli serve\nEnvironment=PYTHONUNBUFFERED=1\n"
            f"EnvironmentFile={p.env_file}\nUMask=0077\nNoNewPrivileges=yes\nMemoryMax={p.memory_max}\n"
            "Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nKillMode=control-group\n\n[Install]\nWantedBy=multi-user.target\n"
        )

    def check(self):
        path = self.plan.systemd_dir / self.plan.unit_name
        if path.is_file():
            same = path.read_text() == self.content()
            return EXISTS, f"{path} present" + ("" if same else " (differs from the template; left untouched)")
        return TODO, f"write {path} with MemoryMax={self.plan.memory_max}"

    def apply(self):
        (self.plan.systemd_dir / self.plan.unit_name).write_text(self.content())
        sh(self.plan, ["systemctl", "daemon-reload"])
        return "created"


class OwnerLogin(Step):
    title = "app login"

    def password_file(self):
        return self.plan.data / "initial-login.txt"

    def check(self):
        db = self.plan.data / "manager.sqlite3"
        if db.is_file():
            try:
                with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
                    row = conn.execute("SELECT username FROM owner").fetchone()
            except sqlite3.Error:
                row = None
            if row:
                return EXISTS, "an owner login already exists; not replaced"
        return TODO, f"create login {self.plan.email} (random password saved 0600 in {self.password_file()})"

    def apply(self):
        p = self.plan
        password = secrets.token_urlsafe(18)
        file = self.password_file()
        file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(f"{p.email}\n{password}\n")
        sh(p, ["chown", f"{p.name}:{p.name}", str(file)])
        sh(p, ["runuser", "-u", p.name, "--", "env", f"AI_MANAGER_DATA={p.data}", f"AI_MANAGER_PROJECT={p.project}",
               "AI_MANAGER_RESTRICTED=1", p.python, "-m", "ai_manager.provision", "create-owner", "--password-file", str(file),
               "--email", p.email])
        return "created"


class StartService(Step):
    title = "service enabled"

    def check(self):
        code, _ = self.plan.run(["systemctl", "is-enabled", self.plan.unit_name])
        return (EXISTS, "already enabled") if code == 0 else (TODO, f"systemctl enable --now {self.plan.unit_name}")

    def apply(self):
        sh(self.plan, ["systemctl", "enable", "--now", self.plan.unit_name])
        return "started"


def acl_entries(plan, path):
    """{'user:x': 'rwx', 'default:user:x': 'rwx'} of one path, or None when it does not exist yet."""
    code, output = plan.run(["getfacl", "-p", "-c", str(path)])
    if code:
        return None
    entries = {}
    for line in output.splitlines():
        parts = line.split("#")[0].strip().split(":")
        if parts[0] in ("user", "default") and len(parts) >= 3 and "user" in parts[:2] and parts[-2]:
            entries[":".join(parts[:-1])] = parts[-1]
    return entries


def acl_missing(plan, wanted):
    """wanted: [(path, entry, perms)]. The entries that are absent or grant less than asked (read/write/execute bits)."""
    missing = []
    cache = {}
    for path, entry, perms in wanted:
        have = cache.setdefault(path, acl_entries(plan, path))
        got = (have or {}).get(entry, "---")
        if any(bit != "-" and got[i] != bit for i, bit in enumerate(perms)):
            missing.append((path, entry, perms))
    return missing


class Acl(Step):
    """Named-user ACL entries on exactly the listed paths; nothing else is opened (no chmod, no group or other bits)."""

    def wanted(self):
        raise NotImplementedError

    def check(self):
        missing = acl_missing(self.plan, self.wanted())
        if not missing:
            return EXISTS, "ACL entries already present"
        return TODO, "setfacl " + ", ".join(sorted({f"{e}:{p}" for _, e, p in missing})) + " on " + ", ".join(sorted({str(x) for x, _, _ in missing}))

    def apply(self):
        for path, entry, perms in acl_missing(self.plan, self.wanted()):
            spec = entry.replace("default:", "d:").replace("user:", "u:")
            recursive = self.recursive(path)
            argv = ["setfacl", *(["-R"] if recursive else []), "-m", f"{spec}:{perms.replace('x', 'X') if recursive else perms}", str(path)]
            sh(self.plan, argv)
        return "created"

    def recursive(self, path):
        return False


class SyncthingAccess(Acl):
    """The Syncthing daemon user reads and writes the new folder only; everything above it just gets search (x)."""
    title = "syncthing user access to the folder"

    def wanted(self):
        p, who = self.plan, self.plan.syncthing_user
        if p.skip_syncthing or not who or who == p.name:
            return []
        return [(p.home, f"user:{who}", "--x"), (p.project.parent, f"user:{who}", "--x"),
                (p.project, f"user:{who}", "rwx"), (p.project, f"default:user:{who}", "rwx"),
                # files Syncthing creates later stay editable by the instance's own user
                (p.project, f"default:user:{p.name}", "rwx")]

    def recursive(self, path):
        return path == self.plan.project


class ServiceAccess(Acl):
    """The manager's own service user reads the instance's data (deploy snapshots, health checks, Users model list)."""
    title = "manager access to the instance data"

    def wanted(self):
        p, who = self.plan, self.plan.service_user
        if not who or who == p.name:
            return []
        wanted = [(p.home, f"user:{who}", "--x")]
        for parent in (p.home / ".local", p.home / ".local/share"):
            wanted.append((parent, f"user:{who}", "--x"))
        return wanted + [(p.data, f"user:{who}", "rwx"), (p.data, f"default:user:{who}", "rwx")]

    def recursive(self, path):
        return path == self.plan.data


class TailnetServe(Step):
    """Publish 127.0.0.1:<port> on the tailnet only (`tailscale serve`, never Funnel). The one access path by default."""
    title = "tailnet serve"

    def target(self):
        return f"http://127.0.0.1:{self.plan.port}"

    def check(self):
        p = self.plan
        url = p.url  # also proves the tailnet hostname is known
        state, detail = serve_state(serve_status(p.run), p.port)
        if state == "funnel":
            return REFUSE, detail + "; onboarding never uses Funnel. Turn it off first: tailscale funnel --https=%d off" % p.port
        if state == "other":
            return REFUSE, f"{detail}, not {self.target()}; refusing to change it"
        if state == "same":
            return EXISTS, f"{url} already serves {self.target()} on the tailnet"
        return TODO, f"tailscale serve --bg --https={p.port} {self.target()} -> {url} (tailnet only, no Funnel)"

    def apply(self):
        p = self.plan
        sh(p, ["tailscale", "serve", "--bg", f"--https={p.port}", self.target()])
        state, detail = serve_state(serve_status(p.run), p.port)
        if state == "funnel":
            raise ProvisionError(detail + "; turn it off: tailscale funnel --https=%d off" % p.port)
        if state != "same":
            raise ProvisionError(f"tailscale serve did not publish {self.target()} on port {p.port}" + (f": {detail}" if detail else ""))
        return "created"


def guard_tunnel(plan, tunnel_name, config):
    """Cloudflare steps edit only a tunnel dedicated to onboarding. Runs in check(), before any write.
    1. Never the production tunnel (by name, whatever else is configured).
    2. The tunnel's name must start with `linko-onboarding`.
    3. Every hostname already in its ingress must be one onboarding created: a hostname of an onboarding record in the
       manager database, this run's own hostname, or an entry of LINKO_ONBOARDING_HOSTNAMES (comma separated allow-list)."""
    if tunnel_name.strip().lower() == PRODUCTION_TUNNEL:
        raise ProvisionError(f"refusing the tunnel '{PRODUCTION_TUNNEL}': it carries production traffic and onboarding never touches it")
    if not tunnel_name.startswith(DEDICATED_PREFIX):
        raise ProvisionError(f"refusing tunnel '{tunnel_name or '?'}': Cloudflare steps need a tunnel dedicated to onboarding "
                             f"(name starting with '{DEDICATED_PREFIX}'); see docs/ONBOARDING.md appendix")
    allowed = set(plan.onboarding_hostnames) | {plan.hostname}
    allowed |= {h.strip().lower() for h in plan.config.get("LINKO_ONBOARDING_HOSTNAMES", "").split(",") if h.strip()}
    foreign = sorted({r["hostname"] for r in config.get("ingress", []) if r.get("hostname") and r["hostname"].lower() not in allowed})
    if foreign:
        raise ProvisionError(f"refusing tunnel '{tunnel_name}': its ingress has hostnames onboarding did not create ({', '.join(foreign[:5])}); "
                             "use a dedicated tunnel or list them in LINKO_ONBOARDING_HOSTNAMES")


def known_hostnames(data):
    """Hostnames of onboarding records in the manager database (read-only; empty when there is none)."""
    db = Path(data) / "manager.sqlite3" if data else None
    if not db or not db.is_file():
        return frozenset()
    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5) as conn:
            rows = conn.execute("SELECT name, domain FROM onboarding WHERE domain != ''").fetchall()
    except sqlite3.Error:
        return frozenset()
    return frozenset(f"{name}.{domain}".lower() for name, domain in rows)


class Cloudflare(Step):
    def api(self, method, path, body=None):
        token = self.plan.config.get("CLOUDFLARE_API_TOKEN", "")
        status, data = self.plan.http(method, CF_API + path, {"Authorization": f"Bearer {token}"}, body)
        if status >= 300 or not data or not data.get("success", True):
            raise ProvisionError(f"Cloudflare {method} {path.split('?')[0]} -> HTTP {status}")
        return data.get("result")

    def need(self, *keys):
        missing = [k for k in keys if not self.plan.config.get(k)]
        if missing:
            raise ProvisionError("missing " + ", ".join(missing) + " (environment or ignored config file)")
        return [self.plan.config[k] for k in keys]

    def tunnel(self):
        """The tunnel's ingress config, after `guard_tunnel` said it is dedicated to onboarding (cached per run)."""
        if not hasattr(self.plan, "_tunnel_config"):
            self.need("CLOUDFLARE_API_TOKEN")
            account, tunnel = self.need("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_TUNNEL_ID")
            base = f"/accounts/{account}/cfd_tunnel/{tunnel}"
            info = self.api("GET", base) or {}
            config = (self.api("GET", base + "/configurations") or {}).get("config") or {}
            guard_tunnel(self.plan, info.get("name") or "", config)
            self.plan._tunnel_config = config
        return self.plan._tunnel_config


class TunnelRoute(Cloudflare):
    title = "cloudflare tunnel route"

    def _path(self):
        account, tunnel = self.need("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_TUNNEL_ID")
        return f"/accounts/{account}/cfd_tunnel/{tunnel}/configurations"

    def check(self):
        self.config = self.tunnel()
        service = f"http://127.0.0.1:{self.plan.port}"
        for rule in self.config.get("ingress", []):
            if rule.get("hostname") == self.plan.hostname:
                if rule.get("service") == service:
                    return EXISTS, f"{self.plan.hostname} already routes to {service}"
                return REFUSE, f"{self.plan.hostname} already routes to {rule.get('service')}, not {service}"
        return TODO, f"add ingress {self.plan.hostname} -> {service}"

    def apply(self):
        ingress = [r for r in self.config.get("ingress", [])]
        rule = {"hostname": self.plan.hostname, "service": f"http://127.0.0.1:{self.plan.port}"}
        catch_all = [r for r in ingress if "hostname" not in r] or [{"service": "http_status:404"}]
        updated = {**self.config, "ingress": [r for r in ingress if "hostname" in r] + [rule] + catch_all[-1:]}
        self.api("PUT", self._path(), {"config": updated})
        return "created"


class DnsRecord(Cloudflare):
    title = "cloudflare dns cname"

    def check(self):
        self.tunnel()
        zone, tunnel = self.need("CLOUDFLARE_ZONE_ID", "CLOUDFLARE_TUNNEL_ID")
        target = f"{tunnel}.cfargotunnel.com"
        records = self.api("GET", f"/zones/{zone}/dns_records?name={self.plan.hostname}") or []
        for record in records:
            if record.get("type") == "CNAME" and record.get("content") == target:
                return EXISTS, f"{self.plan.hostname} -> {target}"
            return REFUSE, f"{self.plan.hostname} already has a {record.get('type')} record to {record.get('content')}"
        return TODO, f"add CNAME {self.plan.hostname} -> {target}"

    def apply(self):
        zone, tunnel = self.need("CLOUDFLARE_ZONE_ID", "CLOUDFLARE_TUNNEL_ID")
        self.api("POST", f"/zones/{zone}/dns_records",
                 {"type": "CNAME", "name": self.plan.hostname, "content": f"{tunnel}.cfargotunnel.com", "proxied": True})
        return "created"


class AccessApp(Cloudflare):
    title = "cloudflare access application"

    def check(self):
        self.tunnel()
        (account,) = self.need("CLOUDFLARE_ACCOUNT_ID")
        for app in self.api("GET", f"/accounts/{account}/access/apps") or []:
            if app.get("domain") == self.plan.hostname:
                return EXISTS, f"access application '{app.get('name')}' already covers {self.plan.hostname}; not modified"
        return TODO, f"create access application for {self.plan.hostname} limited to {self.plan.email}"

    def apply(self):
        (account,) = self.need("CLOUDFLARE_ACCOUNT_ID")
        self.api("POST", f"/accounts/{account}/access/apps", {
            "name": f"linko-{self.plan.name}", "domain": self.plan.hostname, "type": "self_hosted", "session_duration": "24h",
            "policies": [{"name": f"{self.plan.name} owner", "decision": "allow", "include": [{"email": {"email": self.plan.email}}]}]})
        return "created"


class Syncthing(Step):
    def api(self, method, path, body=None):
        key = self.plan.config.get("SYNCTHING_API_KEY", "")
        if not key:
            raise ProvisionError("missing SYNCTHING_API_KEY (environment or ignored config file)")
        status, data = self.plan.http(method, self.plan.syncthing_url + path, {"X-API-Key": key}, body)
        if status >= 300:
            raise ProvisionError(f"Syncthing {method} {path.split('?')[0]} -> HTTP {status}")
        return data


class SyncDevice(Syncthing):
    title = "syncthing device"

    def check(self):
        if not self.plan.syncthing_device:
            if not self.plan.apply:  # preview before the PC has reported in
                return TODO, "add the Windows device (its ID arrives when the PC reports in)"
            raise ProvisionError("the Syncthing device ID of the PC is required (reported by Linko-Setup.ps1)")
        devices = self.api("GET", "/rest/config/devices") or []
        if any(d.get("deviceID") == self.plan.syncthing_device for d in devices):
            return EXISTS, "device already known; its settings are not changed"
        return TODO, f"add device {self.plan.syncthing_device[:7]}…"

    def apply(self):
        d = self.plan.syncthing_device
        self.api("PUT", f"/rest/config/devices/{d}", {"deviceID": d, "name": f"windows-{self.plan.name}", "autoAcceptFolders": False})
        return "created"


class SyncFolder(Syncthing):
    title = "syncthing folder"

    def check(self):
        p = self.plan
        for folder in self.api("GET", "/rest/config/folders") or []:
            if folder.get("id") == p.folder_id:
                return EXISTS, f"folder {p.folder_id} already exists; not changed"
            if Path(folder.get("path", "")).resolve() == p.project.resolve():
                return REFUSE, f"folder {folder.get('id')} already shares {p.project}"
        return TODO, f"share {p.project} as {p.folder_id} (staggered versioning, .git ignored; Windows side: {p.windows_folder})"

    def apply(self):
        p = self.plan
        mine = (self.api("GET", "/rest/system/status") or {}).get("myID")
        devices = [{"deviceID": d} for d in (mine, p.syncthing_device) if d]
        self.api("PUT", f"/rest/config/folders/{p.folder_id}", {
            "id": p.folder_id, "label": f"{p.name} ({p.windows_folder})", "path": str(p.project), "type": "sendreceive",
            "devices": devices, "fsWatcherEnabled": True,
            "versioning": {"type": "staggered", "params": {"maxAge": "2592000", "cleanInterval": "3600"}}})
        self.api("POST", f"/rest/db/ignores?folder={p.folder_id}", {"ignore": [".git", ".git/**"]})
        return "created"


def build_steps(plan):
    steps = [LinuxUser, Directories, GitInit, Port, EnvFile, SystemdUnit, OwnerLogin, StartService]
    if plan.client_ip:  # only once the PC has reported its Tailscale IP; the screen warns while it has not
        steps.insert(steps.index(SystemdUnit), AllowedClients)
    if plan.service_user:
        steps.append(ServiceAccess)
    steps.append(TailnetServe if not plan.domain else None)
    if plan.domain:  # optional appendix: Cloudflare only on an explicit domain
        steps += [TunnelRoute, DnsRecord, AccessApp]
    if not plan.skip_syncthing:
        steps += ([SyncthingAccess] if plan.syncthing_user else []) + [SyncDevice, SyncFolder]
    return [cls(plan) for cls in steps if cls]


def provision(plan, progress=None):
    """Run all checks first, then (with plan.apply) reconcile. Returns the receipt: list of (title, status, detail).
    `progress(rows)` is called with the whole table (finished rows plus the rest as planned) after every applied step."""
    steps = build_steps(plan)
    for step in steps:
        try:
            step.state, step.detail = step.check()
        except ProvisionError as error:
            step.state, step.detail = REFUSE, str(error)
    if any(s.state == REFUSE for s in steps):
        return [(s.title, "refused" if s.state == REFUSE else "not run", s.detail) for s in steps]
    rows = [[s.title, "skipped" if s.state == EXISTS else "planned", s.detail] for s in steps]
    if plan.apply:
        for index, step in enumerate(steps):
            if step.state == EXISTS:
                continue
            try:
                rows[index][1] = step.apply()
            except ProvisionError as error:
                rows[index][1:] = ["failed", str(error)]
                rows = rows[:index + 1]
                break
            finally:
                if progress:
                    progress(rows)
    return [tuple(r) for r in rows]


def print_receipt(receipt, plan, out=print):
    out(("RECEIPT" if plan.apply else "DRY RUN PLAN (nothing changed; add --apply)") + f" for {plan.name} <{plan.email}>")
    for title, status, detail in receipt:
        out(f"  [{status:8}] {title}: {detail}")
    try:
        url = plan.url if plan.port else "-"
    except ProvisionError:
        url = "-"
    out(f"  instance URL {url}  port {plan.port or '-'}")
    out(f"  note: {CREDENTIAL_NOTE}")


def create_owner(email, password_file):
    """Runs as the new Linux user inside its restricted environment (see OwnerLogin.apply)."""
    from .auth import Auth
    from .config import Settings
    from .db import Database

    settings = Settings()
    settings.prepare()
    password = Path(password_file).read_text().splitlines()[1]
    Auth(Database(settings.db_path), settings).setup(email, password)


def read_private(path):
    """Read a spool file without following a symlink planted by the (less privileged) service user."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        return stream.read(65536)


def write_result(directory, name, value, owner):
    """Atomically replace a result file, owned by the service user (we run as root) and private to it."""
    fd, temporary = tempfile.mkstemp(dir=directory, prefix=".result-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.chmod(temporary, 0o600)
        if os.geteuid() == 0:
            os.chown(temporary, owner.pw_uid, owner.pw_gid)
        os.replace(temporary, directory / name)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def take_login(plan):
    """The generated first login as {'email','password'}; the file is deleted as soon as it was read."""
    file = plan.data / "initial-login.txt"
    try:
        email, password = file.read_text().splitlines()[:2]
    except (OSError, ValueError):
        return None
    file.unlink(missing_ok=True)
    return {"email": email, "password": password}


def handle_request(path, host, directory, run=run_command, http=http_json, extra=None):
    """One spooled request, as root: re-validate everything (the file is only as trustworthy as the service user)."""
    owner = pwd.getpwnam(host["service_user"])
    request = json.loads(read_private(path))
    identifier = request.get("id", "")
    if not ID_RE.match(identifier) or path.name != identifier + ".json" or request.get("action") not in ("plan", "apply"):
        raise ProvisionError("malformed request")
    fields = validate_request(request)
    client_ip = str(request.get("tailscale_ip") or "")
    if client_ip and not in_tailnet(client_ip):  # lands in an env file as root: only a Tailscale address
        raise ProvisionError("malformed request")
    config = load_config(host["config_file"], {})
    plan = Plan(name=fields["name"], email=fields["email"], windows_folder=fields["windows_folder"], domain=fields["domain"],
                repo=Path(host["repo"]), python=host["app_python"], home_base=Path(host.get("home_base", "/home")), config=config,
                apply=request["action"] == "apply", memory_max=fields["memory_max"], syncthing_device=fields["device_id"],
                onboarding_hostnames=known_hostnames(host.get("data")), service_user=host["service_user"],
                syncthing_user=config.get("SYNCTHING_USER") or host["service_user"], run=run, http=http, client_ip=client_ip,
                syncthing_url=config.get("SYNCTHING_URL", "http://127.0.0.1:8384"),
                systemd_dir=Path(host.get("systemd_dir", "/etc/systemd/system")), **(extra or {}))
    saved_port = request.get("port")
    plan.port = saved_port if isinstance(saved_port, int) and FIRST_PORT <= saved_port <= LAST_PORT else 0
    result = {"id": identifier, "action": request["action"], "finished": False, "receipt": []}

    def publish(rows):
        write_result(directory, identifier + ".json", {**result, "receipt": rows, "port": plan.port}, owner)

    receipt = provision(plan, publish)
    result.update(finished=True, receipt=receipt, port=plan.port, data=str(plan.data), unit=plan.unit_name, hostname=plan.hostname)
    if plan.apply and all(status not in ("failed", "refused", "not run") for _, status, _ in receipt):
        result["login"] = take_login(plan)
    write_result(directory, identifier + ".json", result, owner)


def tick(data, host_config=HOST_CONFIG, run=run_command, http=http_json, root_required=True, extra=None):
    """Handle every spooled request once. Started by the user path unit through the sudoers rule; safe to repeat."""
    if root_required and os.geteuid() != 0:
        raise ProvisionError("tick runs as root through the sudoers rule (docs/ONBOARDING.md)")
    host = json.loads(Path(host_config).read_text())
    if host.get("data") != str(data):
        raise ProvisionError("data directory does not match the installed configuration")
    root = Path(data) / "onboarding"
    requests, results = root / "requests", root / "results"
    if not requests.is_dir() or not results.is_dir():
        return 0
    handled = 0
    with (root / "tick.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in sorted(requests.glob("*.json")):
            try:
                handle_request(path, host, results, run, http, extra)
            except (ProvisionError, ValueError, OSError, KeyError) as error:
                write_result(results, path.stem + ".json" if ID_RE.match(path.stem) else ".invalid", {
                    "id": path.stem, "finished": True, "receipt": [("request", "failed", str(error)[:300])]},
                    pwd.getpwnam(host["service_user"]))
            path.unlink(missing_ok=True)
            handled += 1
    return handled


def control_service(host, action, name, run=run_command):
    """Stop/start one onboarded instance's unit for coordinated deploys; only units this tool wrote."""
    unit = Path(host.get("systemd_dir", "/etc/systemd/system")) / f"linko-bot-manager-{name}.service"
    if not NAME_RE.match(name) or not unit.is_file() or f"restricted instance {name}\n" not in unit.read_text():
        raise ProvisionError(f"{name} is not an onboarded instance")
    code, output = run(["systemctl", action, unit.name])
    if code:
        raise ProvisionError(output[-300:])


def main(argv=None, environ=None):
    parser = argparse.ArgumentParser(prog="ai_manager.provision")
    parser.add_argument("--data", help="the manager's data directory: required by tick/service (as in the sudoers rule); add-user reads onboarding hostnames from its database")
    parser.add_argument("--host-config", type=Path, default=HOST_CONFIG, help=argparse.SUPPRESS)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick", help="root: handle spooled web-app requests (started through sudo by a user path unit)")
    service = commands.add_parser("service", help="root: stop/start one onboarded instance (deploy coordination)")
    service.add_argument("action", choices=["stop", "start"])
    service.add_argument("name")
    add = commands.add_parser("add-user", help="onboard one restricted user/instance (dry run unless --apply)")
    add.add_argument("--name", required=True)
    add.add_argument("--email", required=True)
    add.add_argument("--windows-folder", required=True)
    add.add_argument("--domain", help="optional Cloudflare appendix only: zone for <name>.<domain> (needs a dedicated tunnel); "
                                     "omit it for the default tailnet-only access")
    add.add_argument("--syncthing-device-id")
    add.add_argument("--client-ip", default="", help="the PC's Tailscale IP: the instance then answers only that client (AI_MANAGER_ALLOWED_CLIENTS)")
    add.add_argument("--memory-max", default="1G")
    add.add_argument("--home-base", type=Path, default=Path("/home"))
    add.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="ignored KEY=VALUE credentials file outside git")
    add.add_argument("--service-user", help="the Linux user the manager runs as: it and the Syncthing daemon get scoped ACL access")
    add.add_argument("--skip-syncthing", action="store_true")
    add.add_argument("--apply", action="store_true", help="make the changes (default is a dry run)")
    add.add_argument("--dry-run", action="store_true", help="explicit dry run (the default)")
    owner = commands.add_parser("create-owner")
    owner.add_argument("--email", required=True)
    owner.add_argument("--password-file", required=True)
    args = parser.parse_args(argv)
    if args.command in ("tick", "service"):
        try:
            if not args.data:
                parser.error("--data is required")
            host = json.loads(args.host_config.read_text())
            if args.command == "tick":
                tick(args.data, args.host_config)
            else:
                if host.get("data") != args.data:
                    raise ProvisionError("data directory does not match the installed configuration")
                control_service(host, args.action, args.name)
        except (ProvisionError, OSError, ValueError) as error:
            print(f"onboarding: {error}", file=sys.stderr)
            return 1
        return 0
    if args.command == "create-owner":
        create_owner(args.email, args.password_file)
        return 0
    config = load_config(args.config, os.environ if environ is None else environ)
    domain = args.domain or ""
    try:
        fields = validate_request({"name": args.name, "email": args.email, "windows_folder": args.windows_folder,
                                   "domain": domain, "memory_max": args.memory_max, "device_id": args.syncthing_device_id})
    except ProvisionError as error:
        parser.error(str(error))
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run exclude each other")
    if args.client_ip and not in_tailnet(args.client_ip):
        parser.error("--client-ip must be a Tailscale address (100.64.0.0/10)")
    plan = Plan(name=args.name, email=args.email, windows_folder=args.windows_folder, domain=domain,
                repo=Path(__file__).resolve().parents[1], python=sys.executable, home_base=args.home_base, config=config,
                apply=args.apply, memory_max=fields["memory_max"], syncthing_device=fields["device_id"],
                onboarding_hostnames=known_hostnames(args.data), skip_syncthing=args.skip_syncthing,
                service_user=args.service_user or "", syncthing_user=config.get("SYNCTHING_USER") or args.service_user or "",
                client_ip=args.client_ip)
    receipt = provision(plan)
    print_receipt(receipt, plan)
    return 1 if any(s in ("failed", "refused") for _, s, _ in receipt) else 0


if __name__ == "__main__":
    raise SystemExit(main())

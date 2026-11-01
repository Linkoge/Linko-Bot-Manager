"""Add User: owner-only screen, one-paste Windows enrollment, privileged host helper. Every external system is mocked:
no real users, sudo, Cloudflare, Tailscale or Syncthing is touched."""
import base64
import getpass
import gzip
import importlib.util
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fake_codex import FakeServer
from fastapi.testclient import TestClient
from test_provision import ROOT, World

from ai_manager import enrollment
from ai_manager import provision as pv
from ai_manager.config import Settings
from ai_manager.web import create_app

DEVICE = "-".join(["MGMTDEV"] * 8)
PC_DEVICE = "-".join(["WINDEVX"] * 8)
TAILNET_HEADERS = {"x-forwarded-for": "100.64.7.9"}
SECRETS = {"TAILSCALE_API_KEY": "tskey-api-" + "SECRETAPIKEY" * 2, "CLOUDFLARE_API_TOKEN": "cf-secret-token-marker",
           "SYNCTHING_API_KEY": "st-secret-key-marker"}
ENV = {**SECRETS, "TAILSCALE_TAILNET": "example.ts.net", "LINKO_MANAGER_URL": "https://manager.example.ts.net", "LINKO_MANAGER_TAILNET_IP": "100.100.0.1",
       "LINKO_SYNCTHING_DEVICE_ID": DEVICE, "CLOUDFLARE_ACCOUNT_ID": "acc",
       "CLOUDFLARE_ZONE_ID": "zone", "CLOUDFLARE_TUNNEL_ID": "t" * 8}
FORM = {"name": "anna", "email": "anna@example.com", "windows_folder": "C:\\Users\\Anna\\Work"}


class Host:
    """The manager, its onboarding config, the fake root helper (tick over a fake host) and the owner's browser."""

    def __init__(self, tmp_path, monkeypatch, env=None, trusted=("testclient",), port=80):
        self.tmp, self.env = tmp_path, dict(ENV if env is None else env)
        for name in [n for n in os.environ if n.startswith(("AI_MANAGER_", "TAILSCALE_", "LINKO_", "CLOUDFLARE_", "SYNCTHING_"))]:
            monkeypatch.delenv(name)
        monkeypatch.setenv("HOME", str(tmp_path / "service-home"))
        (tmp_path / "service-home/.ssh").mkdir(parents=True)
        self.pubkey = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIManagerKeyManagerKeyManagerKey manager@host"
        (tmp_path / "service-home/.ssh/id_ed25519.pub").write_text(self.pubkey + "\n")
        self.config_file = tmp_path / "onboarding.env"
        self.write_env(self.env)
        root = tmp_path / "projects"
        root.mkdir()
        self.settings = Settings(data_dir=tmp_path / "data", project_roots=(root,), secure_cookie=False,
                                 trusted_proxies=trusted, onboarding_config=self.config_file,
                                 port=port)  # the TestClient's own listener port (the enroll route checks it)
        self.settings.prepare()
        (self.settings.data_dir / "deploy").mkdir()
        (self.settings.data_dir / "deploy/installed.json").write_text(json.dumps({"repo": str(ROOT), "data": str(self.settings.data_dir), "instances": []}))
        for name in ("requests", "results"):
            (self.settings.data_dir / "onboarding" / name).mkdir(parents=True)
        (self.settings.data_dir / "onboarding/installed.json").write_text("{}")
        self.host_config = tmp_path / "onboarding.json"
        self.host_config.write_text(json.dumps({
            "service_user": getpass.getuser(), "data": str(self.settings.data_dir), "repo": str(ROOT), "app_python": "/venv/bin/python",
            "launcher": "/usr/local/lib/linko/onboarding-launcher.py", "config_file": str(self.config_file),
            "home_base": str(tmp_path / "home"), "systemd_dir": str(tmp_path / "units")}))
        (tmp_path / "home").mkdir()
        (tmp_path / "units").mkdir()
        self.world = World(tmp_path)
        self.tailscale = []
        self.app = create_app(self.settings, FakeServer(self.settings))
        self.onboarding = self.app.state.onboarding
        self.onboarding.host_config = self.host_config
        self.onboarding.http = self.fake_tailscale
        self.owner = {"username": "owner", "password": secrets.token_urlsafe(16)}
        self.client = TestClient(self.app, base_url="http://localhost")

    def write_env(self, values):
        self.config_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))

    def fake_tailscale(self, method, url, headers=None, body=None):
        self.tailscale.append((method, url, headers, body))
        return 200, {"key": "tskey-auth-kSingleUse12345"}

    def sign_in(self, client=None):
        client = client or self.client
        if not client.get("/api/auth-status").json()["authenticated"]:
            token = (self.settings.data_dir / "setup-token")
            if token.exists():
                assert client.post("/api/setup", json={**self.owner, "token": token.read_text()}).status_code == 200
            client.headers["X-CSRF-Token"] = client.post("/api/login", json=self.owner).json()["csrf"]

    def tick(self):
        return pv.tick(self.settings.data_dir, self.host_config, run=self.world.run, http=self.world.http, root_required=False,
                       extra={"port_free": lambda port: True})

    def create(self, **over):
        response = self.client.post("/api/onboarding", json={**FORM, **over})
        assert response.status_code == 200, response.text
        return response.json()

    def get(self, record):
        return self.client.get(f"/api/onboarding/{record}").json()

    def command(self, record, **body):
        response = self.client.post(f"/api/onboarding/{record}/enrollment", json=body)
        assert response.status_code == 200, response.text
        return response.json()

    def report(self, token, headers=TAILNET_HEADERS, **over):
        body = {"token": token, "hostname": "ANNA-PC", "tailscale_ip": "100.101.102.103", "device_id": PC_DEVICE, **over}
        return self.plain().post("/api/enroll/report", json=body, headers=headers)

    def plain(self):
        """A browser with no session cookie: what the enrolled PC is."""
        return TestClient(self.app, base_url="http://localhost")

    def token_of(self, command):
        return re.search(r"-EnrollmentToken '([^']+)'", command).group(1)

    def database_text(self):
        with sqlite3.connect(self.settings.db_path) as conn:
            return "\n".join(str(row) for row in conn.iterdump())


@pytest.fixture
def host(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    with h.client:
        h.sign_in()
        yield h


@pytest.fixture
def no_subprocess(monkeypatch):
    """The web app never starts a process for onboarding (and so never sudo)."""
    def refuse(*a, **k):
        raise AssertionError(f"web app started a process: {a}")
    monkeypatch.setattr(subprocess, "run", refuse)
    monkeypatch.setattr(subprocess, "Popen", refuse)


# ---- the screen: owner-only, absent on restricted instances ------------------------------------------------------------
@pytest.mark.smoke
def test_screen_requires_the_owner_and_is_absent_on_restricted_instances(tmp_path, monkeypatch):
    (tmp_path / "main").mkdir()
    h = Host(tmp_path / "main", monkeypatch)
    with h.client:
        assert h.client.get("/api/onboarding").status_code == 401
        assert h.client.post("/api/onboarding", json=FORM).status_code == 401
        h.sign_in()
        assert h.client.get("/api/onboarding").status_code == 200
    work = tmp_path / "work"
    project = work / "folder"
    project.mkdir(parents=True)
    settings = Settings(data_dir=work / "data", project_roots=(work,), restricted=True, project_path=project, secure_cookie=False,
                        trusted_proxies=("testclient",))
    with TestClient(create_app(settings, FakeServer(settings)), base_url="http://localhost") as restricted:
        token, owner = (settings.data_dir / "setup-token").read_text(), {"username": "o", "password": secrets.token_urlsafe(16)}
        restricted.post("/api/setup", json={**owner, "token": token})
        restricted.headers["X-CSRF-Token"] = restricted.post("/api/login", json=owner).json()["csrf"]
        assert restricted.get("/api/onboarding").status_code == 403
        assert restricted.post("/api/onboarding", json=FORM).status_code == 403
        assert restricted.get("/api/onboarding/x").status_code == 403
        body = {"token": "t" * 30, "hostname": "PC", "tailscale_ip": "100.64.1.1", "device_id": PC_DEVICE}
        assert restricted.post("/api/enroll/report", json=body, headers=TAILNET_HEADERS).status_code == 404


def test_button_and_dialog_are_hidden_for_restricted_instances():
    static = ROOT / "ai_manager/static"
    app_js, page, css, js = ((static / n).read_text() for n in ("app.js", "index.html", "style.css", "onboarding.js"))
    assert 'id="onboarding-dialog"' in page and "/static/onboarding.js" in page
    # the menu entry is built inside the one block that only runs when the instance is not restricted
    assert re.search(r"if\(!state\.restricted\)\{const ideas=.*?openOnboarding", app_js)
    assert ".restricted #onboarding-dialog" in css
    assert re.search(r"async function openOnboarding\(\)\{\s*if\(state\.restricted\)return;", js)


# ---- preview and privilege ----------------------------------------------------------------------------------------------
def test_preview_has_no_side_effects_and_runs_without_the_web_app_starting_a_process(host, no_subprocess):
    record = host.create()
    assert record["state"] == "pending" and record["busy"] and record["action"] == "plan"
    spooled = json.loads(next((host.settings.data_dir / "onboarding/requests").glob("*.json")).read_text())
    assert spooled["action"] == "plan" and spooled["name"] == "anna" and "password" not in json.dumps(spooled)
    assert host.tick() == 1
    shown = host.get(record["id"])
    assert shown["state"] == "pending" and not shown["busy"]
    assert {s["status"] for s in shown["steps"]} == {"planned"} and len(shown["steps"]) >= 10
    assert host.world.mutating() == [] and not list((host.tmp / "home").iterdir()) and not list((host.tmp / "units").iterdir())
    assert not [c for c in host.world.calls if c[0] in ("PUT", "POST")]
    assert not list((host.settings.data_dir / "onboarding/requests").glob("*.json"))


def test_apply_without_the_installed_helper_says_how_to_install(host, tmp_path):
    (host.settings.data_dir / "onboarding/installed.json").unlink()
    response = host.client.post("/api/onboarding", json=FORM)
    assert response.status_code == 409 and "install_onboarding.py" in response.json()["detail"]


def test_tick_runs_as_root_only_and_checks_the_data_directory(host):
    if os.geteuid() != 0:
        with pytest.raises(pv.ProvisionError, match="root"):
            pv.tick(host.settings.data_dir, host.host_config)
    with pytest.raises(pv.ProvisionError, match="does not match"):
        pv.tick(host.tmp / "elsewhere", host.host_config, root_required=False)


@pytest.mark.parametrize("bad", [
    {"name": "Root;rm"}, {"name": "../etc"}, {"email": "a b@c.d"}, {"windows_folder": "C:\\a'b"}, {"memory_max": "1G;x"},
    {"action": "drop"}, {"id": "../../x"}])
def test_tick_rejects_forged_requests_before_running_anything(host, bad):
    record = host.create()
    host.tick()
    path = host.settings.data_dir / "onboarding/requests" / f"{record['id']}.json"
    request = {"id": record["id"], "action": "apply", "name": "anna", "email": "anna@example.com", "windows_folder": "C:\\W",
               "domain": "example.com", "memory_max": "1G", "device_id": PC_DEVICE, **bad}
    path.write_text(json.dumps(request))
    host.world.calls.clear()
    host.tick()
    assert host.world.calls == []
    result = json.loads((host.settings.data_dir / "onboarding/results" / f"{record['id']}.json").read_text())
    assert result["finished"] and result["receipt"][0][1] == "failed"


def test_tick_does_not_follow_symlinked_requests(host):
    record = host.create()
    host.tick()
    path = host.settings.data_dir / "onboarding/requests" / f"{record['id']}.json"
    target = host.tmp / "secret.txt"
    target.write_text("do not read me")
    path.symlink_to(target)
    host.tick()
    result = (host.settings.data_dir / "onboarding/results" / f"{record['id']}.json").read_text()
    assert "do not read me" not in result and json.loads(result)["receipt"][0][1] == "failed"


def test_launcher_service_command_only_touches_units_it_wrote(tmp_path):
    units = tmp_path / "units"
    units.mkdir()
    (units / "linko-bot-manager-anna.service").write_text("[Unit]\nDescription=Linko Bot Boss restricted instance anna\n")
    (units / "linko-bot-manager-bob.service").write_text("[Unit]\nDescription=something else\n")
    calls = []
    host = {"systemd_dir": str(units)}
    pv.control_service(host, "stop", "anna", run=lambda argv: calls.append(argv) or (0, ""))
    assert calls == [["systemctl", "stop", "linko-bot-manager-anna.service"]]
    for name in ("bob", "ghost", "../anna", "ssh"):
        with pytest.raises(pv.ProvisionError):
            pv.control_service(host, "stop", name, run=lambda argv: calls.append(argv) or (0, ""))
    assert len(calls) == 1


# ---- enrollment: the one-liner ---------------------------------------------------------------------------------------------
@pytest.mark.smoke
def test_one_liner_embeds_the_script_and_only_single_use_credentials(host):
    record = host.create()
    result = host.command(record["id"])
    command = result["command"]
    token = host.token_of(command)
    # the exact repository script travels inside the line; nothing is downloaded
    packed = re.search(r"FromBase64String\('([^']+)'\)", command).group(1)
    assert gzip.decompress(base64.b64decode(packed)).decode() == (ROOT / "deploy/windows/Linko-Setup.ps1").read_text()
    assert not re.search(r"https?://(?!manager\.example\.ts\.net)", command.replace(packed, ""))
    assert "Invoke-WebRequest" not in command and "iwr" not in command.lower().split()
    arguments = dict(re.findall(r"-(\w+) '([^']*)'", command))
    assert arguments == {"TailscaleAuthKey": "tskey-auth-kSingleUse12345", "ManagementPublicKey": host.pubkey,
                         "WindowsFolder": FORM["windows_folder"], "ManagerUrl": "https://manager.example.ts.net",
                         "ManagerDeviceId": DEVICE, "FolderId": "linko-anna", "EnrollmentToken": token}
    for secret in SECRETS.values():
        assert secret not in command and secret not in json.dumps(result)
    assert not re.search(r"tskey-api|cloudflare|CLOUDFLARE|SYNCTHING_API", command + gzip.decompress(base64.b64decode(packed)).decode())
    # the key is minted single-use, preauthorized, tagged, short-lived, with the API key as bearer credential
    method, url, headers, body = host.tailscale[-1]
    create = body["capabilities"]["devices"]["create"]
    assert method == "POST" and url.endswith("/tailnet/example.ts.net/keys") and headers["Authorization"] == f"Bearer {SECRETS['TAILSCALE_API_KEY']}"
    assert create["reusable"] is False and create["preauthorized"] is True and create["tags"] and 0 < body["expirySeconds"] <= 7200
    assert host.get(record["id"])["state"] == "enrolling"
    assert abs(result["expires_at"] - time.time() - 7200) < 60


def test_token_is_stored_hashed_only_and_a_new_command_replaces_it(host):
    record = host.create()
    first = host.token_of(host.command(record["id"])["command"])
    assert first not in host.database_text() and enrollment.digest(first) in host.database_text()
    second = host.token_of(host.command(record["id"])["command"])
    assert first != second and host.report(first).status_code == 403
    assert host.report(second).status_code == 200


def test_without_a_tailscale_api_key_the_owner_pastes_a_key_that_is_not_stored(host):
    host.write_env({k: v for k, v in ENV.items() if k != "TAILSCALE_API_KEY"})
    record = host.create()
    refused = host.client.post(f"/api/onboarding/{record['id']}/enrollment", json={})
    assert refused.status_code == 409 and refused.json()["needs_auth_key"] is True and host.tailscale == []
    bad = host.client.post(f"/api/onboarding/{record['id']}/enrollment", json={"auth_key": "tskey-api-nope; evil"})
    assert bad.status_code == 409 and "evil" not in bad.text
    pasted = "tskey-auth-kPastedSingleUse99"
    command = host.command(record["id"], auth_key=pasted)["command"]
    assert f"-TailscaleAuthKey '{pasted}'" in command and host.tailscale == []
    assert pasted not in host.database_text()


def test_hostile_values_cannot_break_out_of_the_command(host):
    for folder in ("C:\\a'; Remove-Item C:\\ -Recurse; '", "C:\\a$(calc)", "C:\\a\u2019b", "C:\\a`nb"):
        response = host.client.post("/api/onboarding", json={**FORM, "name": "bob", "windows_folder": folder})
        assert response.status_code == 409, folder
    with pytest.raises(pv.ProvisionError):
        enrollment.build_command(auth_key="tskey-auth-abcdefghij", public_key=host.pubkey, windows_folder="C:\\a'b",
                                 url="https://m.ts.net", manager_device=DEVICE, token="t" * 40, folder_id="linko-anna")
    assert enrollment.ps_quote("it's") == "'it''s'"


# ---- enrollment: the report endpoint -----------------------------------------------------------------------------------------
@pytest.mark.smoke
def test_report_is_single_use_and_rejects_wrong_reused_and_expired_tokens(host):
    record = host.create()
    token = host.token_of(host.command(record["id"])["command"])
    assert host.report("x" * 43).status_code == 403
    assert host.report(token[:-1] + ("A" if token[-1] != "A" else "B")).status_code == 403
    accepted = host.report(token)
    assert accepted.status_code == 200 and accepted.json() == {"accepted": True}
    shown = host.get(record["id"])
    assert (shown["device_id"], shown["pc_hostname"], shown["tailscale_ip"]) == (PC_DEVICE, "ANNA-PC", "100.101.102.103")
    assert shown["state"] == "enrolling"
    assert host.report(token, device_id="-".join(["EVILDEV"] * 8)).status_code == 403  # reused
    assert host.get(record["id"])["device_id"] == PC_DEVICE
    # expiry: two hours
    fresh = host.token_of(host.command(record["id"])["command"])
    host.onboarding.now = lambda: time.time() + 2 * 3600 + 5
    assert host.report(fresh).status_code == 403
    host.onboarding.now = time.time
    assert host.report(host.token_of(host.command(record["id"])["command"])).status_code == 200


def test_report_accepts_only_that_report_from_the_tailnet(host):
    record = host.create()
    token = host.token_of(host.command(record["id"])["command"])
    pc = host.plain()
    body = {"token": token, "hostname": "ANNA-PC", "tailscale_ip": "100.101.102.103", "device_id": PC_DEVICE}
    assert pc.post("/api/enroll/report", json=body).status_code == 403  # not from the tailnet (direct peer only)
    assert pc.post("/api/enroll/report", json=body, headers={"x-forwarded-for": "8.8.8.8"}).status_code == 403
    assert pc.post("/api/enroll/report", json=body, headers={**TAILNET_HEADERS, "cf-connecting-ip": "100.64.7.9"}).status_code == 403
    assert pc.post("/api/enroll/report", json=body, headers={**TAILNET_HEADERS, "cf-ray": "abc"}).status_code == 403
    assert pc.post("/api/enroll/report", json={**body, "extra": "x"}, headers=TAILNET_HEADERS).status_code == 422
    assert pc.post("/api/enroll/report", json={**body, "tailscale_ip": "10.0.0.5"}, headers=TAILNET_HEADERS).status_code == 422
    assert pc.post("/api/enroll/report", json={**body, "device_id": "nope"}, headers=TAILNET_HEADERS).status_code == 422
    assert pc.get("/api/enroll/report", headers=TAILNET_HEADERS).status_code == 405
    assert token not in pc.post("/api/enroll/report", json={**body, "extra": 1}, headers=TAILNET_HEADERS).text
    # none of the rejected requests used up the token
    assert pc.post("/api/enroll/report", json=body, headers=TAILNET_HEADERS).status_code == 200


def test_report_is_throttled_per_source(host):
    pc = host.plain()
    body = {"token": "w" * 43, "hostname": "PC", "tailscale_ip": "100.101.102.103", "device_id": PC_DEVICE}
    codes = [pc.post("/api/enroll/report", json=body, headers=TAILNET_HEADERS).status_code for _ in range(12)]
    assert codes[:10] == [403] * 10 and codes[10:] == [429, 429]


def test_report_after_provisioning_started_cannot_change_anything(host):
    record = host.create()
    host.tick()
    token = host.token_of(host.command(record["id"])["command"])
    assert host.client.patch(f"/api/onboarding/{record['id']}", json={"device_id": PC_DEVICE}).status_code == 200
    assert host.client.post(f"/api/onboarding/{record['id']}/apply").json()["state"] == "provisioning"
    assert host.report(token, device_id="-".join(["LATEDEV"] * 8)).status_code == 403
    assert host.client.post(f"/api/onboarding/{record['id']}/enrollment", json={}).status_code == 409
    assert host.get(record["id"])["device_id"] == PC_DEVICE


@pytest.mark.smoke
def test_no_route_is_reachable_without_a_session_except_the_known_public_ones(host):
    public = {("GET", "/"), ("GET", "/api/auth-status"), ("GET", "/manifest.webmanifest"), ("POST", "/api/login"),
              ("POST", "/api/setup"), ("POST", "/api/enroll/report")}
    anonymous = host.plain()
    seen = set()
    for route in host.app.routes:
        path = getattr(route, "path", "")
        if not path.startswith("/api"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            seen.add((method, path))
            if (method, path) in public:
                continue
            url = re.sub(r"\{[^}]+\}", "x", path)
            status = anonymous.request(method, url, json={}).status_code
            assert status == 401, (method, path, status)
    assert ("POST", "/api/enroll/report") in seen and ("GET", "/api/onboarding") in seen
    # the report is the only public route that is new, and it is a POST of exactly one report
    assert {s for s in seen if s[1].startswith(("/api/enroll", "/api/onboarding"))} >= {("POST", "/api/enroll/report")}


# ---- provisioning through the helper ------------------------------------------------------------------------------------------
@pytest.mark.smoke
def test_full_flow_record_receipt_login_shown_once_and_file_deleted(host, no_subprocess):
    record = host.create()
    host.tick()
    assert host.client.post(f"/api/onboarding/{record['id']}/apply").status_code == 409  # the PC has not reported in
    token = host.token_of(host.command(record["id"])["command"])
    assert host.report(token).status_code == 200
    applied = host.client.post(f"/api/onboarding/{record['id']}/apply").json()
    assert applied["state"] == "provisioning" and applied["busy"] and not applied["token_active"]
    host.tick()
    done = host.get(record["id"])
    assert done["state"] == "done" and not done["busy"] and done["error"] == ""
    titles = {s["title"]: s["status"] for s in done["steps"]}
    assert titles["linux user"] == "created" and titles["deploy registration"] == "created"
    assert titles["syncthing user access to the folder"] == "created" and titles["manager access to the instance data"] == "created"
    assert titles["syncthing device"] == "created" and titles["syncthing folder"] == "created"
    assert any(d["deviceID"] == PC_DEVICE for d in host.world.st_devices)
    # deploy registration: the instance is listed with coordinated stop/start through the launcher
    instance = json.loads((host.settings.data_dir / "deploy/installed.json").read_text())["instances"][0]
    assert instance["name"] == "anna" and instance["service"] == "linko-bot-manager-anna.service"
    assert instance["url"].startswith("http://127.0.0.1:") and instance["data"].endswith("linko-bot-manager-anna")
    assert instance["service_cmds"]["stop"] == ["/usr/bin/sudo", "-n", "/usr/bin/python3", "-I", "/usr/local/lib/linko/onboarding-launcher.py",
                                                "--data", str(host.settings.data_dir), "service", "stop", "anna"]
    # the first login: delivered once, initial-login.txt gone, nothing left in the database
    login_file = host.tmp / "home/anna/.local/share/linko-bot-manager-anna/initial-login.txt"
    assert not login_file.exists()
    assert done["has_login"]
    shown = host.client.post(f"/api/onboarding/{record['id']}/login")
    assert shown.status_code == 200 and shown.json()["email"] == "anna@example.com" and len(shown.json()["password"]) >= 20
    assert host.client.post(f"/api/onboarding/{record['id']}/login").status_code == 409
    assert shown.json()["password"] not in json.dumps(done)  # the receipt never carries it
    assert shown.json()["password"] not in host.database_text() and not host.get(record["id"])["has_login"]
    assert not list((host.settings.data_dir / "onboarding/results").glob("*.json"))
    assert not login_file.exists()
    for forbidden in SECRETS.values():
        assert forbidden not in host.database_text()


def test_auto_apply_starts_provisioning_when_the_pc_reports_in(host):
    record = host.create(auto_apply=True)
    host.tick()
    assert host.report(host.token_of(host.command(record["id"])["command"])).status_code == 200
    shown = host.get(record["id"])
    assert shown["state"] == "provisioning" and shown["busy"] and shown["action"] == "apply"
    assert json.loads(next((host.settings.data_dir / "onboarding/requests").glob("*.json")).read_text())["device_id"] == PC_DEVICE
    host.tick()
    assert host.get(record["id"])["state"] == "done"


def test_failure_is_recorded_and_retry_reruns_the_idempotent_command(host):
    host.world.ingress.insert(0, {"hostname": "anna.example.com", "service": "http://127.0.0.1:8001"})
    record = host.create(domain="example.com")  # the optional Cloudflare appendix
    host.tick()
    assert host.report(host.token_of(host.command(record["id"])["command"])).status_code == 200
    host.client.post(f"/api/onboarding/{record['id']}/apply")
    host.tick()
    failed = host.get(record["id"])
    assert failed["state"] == "failed" and "cloudflare tunnel route" in failed["error"]
    assert host.world.mutating() == []  # all checks run before any change
    host.world.ingress.pop(0)
    assert host.client.post(f"/api/onboarding/{record['id']}/apply").json()["state"] == "provisioning"
    host.tick()
    assert host.get(record["id"])["state"] == "done"
    assert host.client.post(f"/api/onboarding/{record['id']}/apply").status_code == 409  # done is final
    calls = len(host.world.calls)
    host.onboarding.update(record["id"], state="failed")  # a second Retry on a finished machine only skips
    host.client.post(f"/api/onboarding/{record['id']}/apply")
    host.tick()
    assert {s["status"] for s in host.get(record["id"])["steps"] if s["title"] != "deploy registration"} == {"skipped"}
    assert not [c for c in host.world.calls[calls:] if c[0] == "run" and c[1][0] in ("useradd", "setfacl", "chown")]


def test_a_helper_that_never_answers_fails_the_record_instead_of_hanging(host):
    record = host.create()
    host.onboarding.now = lambda: time.time() + 3600
    assert host.get(record["id"])["steps"][0]["status"] == "failed" and not host.get(record["id"])["busy"]


def test_progress_is_visible_while_provisioning(host, monkeypatch):
    record = host.create()
    host.tick()
    assert host.report(host.token_of(host.command(record["id"])["command"])).status_code == 200
    host.client.post(f"/api/onboarding/{record['id']}/apply")
    seen, original = [], pv.provision

    def watching(plan, progress=None):
        return original(plan, lambda rows: (progress(rows), seen.append(host.get(record["id"]))))

    monkeypatch.setattr(pv, "provision", watching)
    host.tick()
    middle = [s for s in seen if s["state"] == "provisioning" and s["steps"]]
    assert len(middle) > 3 and {x["status"] for x in middle[0]["steps"]} >= {"planned"}
    assert middle[0]["steps"][0]["status"] == "created" and host.get(record["id"])["state"] == "done"


def test_records_survive_a_reload_and_names_are_unique(host):
    record = host.create()
    host.tick()
    again = TestClient(create_app(host.settings, FakeServer(host.settings)), base_url="http://localhost")  # a fresh process
    host.sign_in(again)
    listed = again.get("/api/onboarding").json()["entries"]
    assert [(e["name"], e["email"], e["state"]) for e in listed] == [("anna", "anna@example.com", "pending")]
    assert listed[0]["steps"] and listed[0]["id"] == record["id"]
    assert again.post("/api/onboarding", json=FORM).status_code == 409


def test_records_only_ever_hold_one_of_five_states(host):
    host.create()
    with sqlite3.connect(host.settings.db_path) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE onboarding SET state='waiting'")
        schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='onboarding'").fetchone()[0]
    assert set(re.findall(r"'(\w+)'", re.search(r"state TEXT.*?CHECK\((.*?)\)", schema, re.S).group(1))) == {
        "pending", "enrolling", "provisioning", "done", "failed"}


def test_the_web_app_side_never_starts_processes_or_changes_owners():
    text = (ROOT / "ai_manager/onboarding.py").read_text()
    assert "subprocess" not in text and "os.system" not in text and "chown" not in text and "setuid" not in text


# ---- the one-time installer ------------------------------------------------------------------------------------------------
def installer():
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location("install_onboarding", ROOT / "scripts/install_onboarding.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_installer_plan_allows_exactly_the_launcher_commands(tmp_path):
    mod = installer()
    layout = mod.Layout(lib=tmp_path / "lib", etc=tmp_path / "etc", sudoers_dir=tmp_path / "sudoers.d")
    data = tmp_path / "svc/.local/share/linko-bot-manager"
    steps = mod.install_plan("svc", data, ROOT, tmp_path / "svc/.config/linko-onboarding.env", layout)
    by_path = {str(s.detail.get("path")): s for s in steps if s.kind in ("write", "sudoers")}
    launcher = by_path[str(layout.lib / "onboarding-launcher.py")]
    assert launcher.detail["content"] == (ROOT / "ai_manager/provision.py").read_text()
    assert launcher.detail["owner"] == "root:root" and launcher.detail["mode"] == 0o755
    sudoers = by_path[str(layout.sudoers_dir / "linko-onboarding")].detail
    assert sudoers["mode"] == 0o440 and sudoers["owner"] == "root:root"
    rules = [line for line in sudoers["content"].splitlines() if "NOPASSWD" in line]
    assert rules == ["svc ALL=(root) NOPASSWD: LINKO_ONBOARD"]
    alias = next(line for line in sudoers["content"].splitlines() if line.startswith("Cmnd_Alias"))
    base = f"/usr/bin/python3 -I {layout.lib / 'onboarding-launcher.py'} --data {data}"
    assert alias == f"Cmnd_Alias LINKO_ONBOARD = {base} tick, {base} service stop *, {base} service start *"
    config = json.loads(by_path[str(layout.etc / "onboarding.json")].detail["content"])
    assert config["service_user"] == "svc" and config["data"] == str(data) and config["app_python"].endswith(".venv/bin/python")
    with pytest.raises(ValueError):
        mod.install_plan("root", data, ROOT, None, layout)
    with pytest.raises(ValueError):
        mod.install_plan("svc", Path("/tmp/a b"), ROOT, None, layout)


def test_activate_installs_user_units_that_match_the_sudoers_rule(tmp_path, monkeypatch):
    mod = installer()
    layout = mod.Layout(lib=tmp_path / "lib", etc=tmp_path / "etc", sudoers_dir=tmp_path / "sudoers.d")
    data, home = tmp_path / "data", tmp_path / "home"
    data.mkdir()
    with pytest.raises(SystemExit, match="install"):
        mod.activate(ROOT, data, home, layout, enable=False)
    monkeypatch.setattr(mod, "installed", lambda *a, **k: True)
    mod.activate(ROOT, data, home, layout, enable=False)
    service = (home / ".config/systemd/user/linko-bot-manager-onboarding.service").read_text()
    path_unit = (home / ".config/systemd/user/linko-bot-manager-onboarding.path").read_text()
    command = " ".join(mod.tick_command(data, layout))
    assert f"ExecStart={command}\n" in service and "NoNewPrivileges" not in service  # sudo cannot work under NoNewPrivileges
    assert f"DirectoryNotEmpty={data}/onboarding/requests" in path_unit and "linko-bot-manager-onboarding.service" in path_unit
    assert (data / "onboarding/installed.json").is_file() and oct((data / "onboarding/requests").stat().st_mode & 0o777) == "0o700"


def test_register_instances_keeps_hand_edited_entries(tmp_path):
    sys.path.insert(0, str(ROOT / "scripts"))
    import install_deploy
    data = tmp_path / "data"
    (data / "deploy").mkdir(parents=True)
    (data / "deploy/installed.json").write_text(json.dumps({"repo": "r", "instances": [{"name": "old", "service_cmds": {"stop": ["x"]}}]}))
    entry = {"name": "anna", "service": "u", "data": "/d", "url": "http://127.0.0.1:8788"}
    assert install_deploy.register_instances(data, [entry]) is True
    assert install_deploy.register_instances(data, [entry]) is False
    names = [i["name"] for i in json.loads((data / "deploy/installed.json").read_text())["instances"]]
    assert names == ["old", "anna"]
    with pytest.raises(ValueError):
        install_deploy.register_instances(tmp_path / "none", [entry])

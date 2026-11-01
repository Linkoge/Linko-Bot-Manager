"""Public access through a tunnel (example hostname): cookies, forwarded client IP, hosts, setup, headers."""

import os

import pytest
from fake_codex import FakeServer
from fastapi.testclient import TestClient

from ai_manager.config import Settings
from ai_manager.proxy import client_ip, is_https, parse_trusted
from ai_manager.web import create_app

OWNER_SECRET = "-".join(["throwaway", "owner", "value", "1"])  # disposable, built at runtime
PUBLIC = "public.example"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ambient AI_MANAGER_* settings of the machine running the tests must not change what these tests see."""
    for name in [n for n in os.environ if n.startswith("AI_MANAGER_")]:
        monkeypatch.delenv(name)


def make(tmp_path, **options):
    root = tmp_path / "projects"
    root.mkdir(parents=True, exist_ok=True)
    settings = Settings(data_dir=tmp_path / "data", project_roots=(root,), **options)
    app = create_app(settings, FakeServer(settings))
    return settings, app


@pytest.fixture
def tunnel(tmp_path):
    # The TestClient's direct peer is "testclient"; treat it as the local tunnel daemon.
    settings, app = make(tmp_path, secure_cookie=False, trusted_proxies=("testclient",), public_hosts=(PUBLIC,))
    with TestClient(app, base_url=f"https://{PUBLIC}") as client:
        yield client, app, settings


def create_owner(client, settings, **headers):
    token = (settings.data_dir / "setup-token").read_text()
    response = client.post("/api/setup", json={"username": "owner", "password": OWNER_SECRET, "token": token},
                           headers=headers)
    assert response.status_code == 200


def attempts(app):
    """Attempts per address, ignoring the owner-creation call made by the test itself from the proxy address."""
    rows = app.state.db.all("SELECT ip,count(*) AS n FROM login_attempts WHERE ip!='testclient' GROUP BY ip")
    return {r["ip"]: r["n"] for r in rows}


def test_public_host_comes_from_configuration_and_existing_hosts_keep_working(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MANAGER_ALLOWED_HOSTS", "localhost,box.tail1234.ts.net")
    monkeypatch.setenv("AI_MANAGER_PUBLIC_HOSTS", PUBLIC)
    settings, app = make(tmp_path)
    assert {PUBLIC, "localhost", "box.tail1234.ts.net"} <= set(settings.allowed_hosts)
    for host in (PUBLIC, "box.tail1234.ts.net", "localhost"):
        with TestClient(app, base_url=f"https://{host}") as client:
            assert client.get("/api/auth-status").status_code == 200
    with TestClient(app, base_url="https://evil.example") as client:
        assert client.get("/api/auth-status").status_code == 400
    with TestClient(app, base_url=f"https://{PUBLIC}", headers={"Host": f"{PUBLIC}.evil.example"}) as client:
        assert client.get("/api/auth-status").status_code == 400


def test_there_is_no_built_in_public_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MANAGER_ALLOWED_HOSTS", "localhost,box.tail1234.ts.net")
    settings, app = make(tmp_path)
    assert settings.public_hosts == ("",)
    assert set(settings.allowed_hosts) == {"localhost", "box.tail1234.ts.net"}
    with TestClient(app, base_url=f"https://{PUBLIC}") as client:
        assert client.get("/api/auth-status").status_code == 400
    with TestClient(app, base_url="https://box.tail1234.ts.net") as client:
        assert client.get("/api/auth-status").status_code == 200


def test_public_hosts_are_configurable(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_MANAGER_PUBLIC_HOSTS", "a.example, b.example")
    assert {"a.example", "b.example"} <= set(Settings(data_dir=tmp_path).allowed_hosts)


def test_origin_from_public_host_is_accepted_and_foreign_origin_rejected(tunnel):
    client, app, settings = tunnel
    create_owner(client, settings)
    csrf = client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET}).json()["csrf"]
    ok = client.post("/api/projects/discover", headers={"X-CSRF-Token": csrf, "Origin": f"https://{PUBLIC}"})
    assert ok.status_code == 200
    bad = client.post("/api/projects/discover", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert bad.status_code == 403


def session_cookie(response):
    return next(v for v in response.headers.get_list("set-cookie") if v.startswith("ai_manager_session="))


@pytest.mark.smoke
def test_session_cookie_flags_over_https_and_logout_expiry(tunnel):
    client, app, settings = tunnel
    create_owner(client, settings)
    response = client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET},
                           headers={"X-Forwarded-Proto": "https"})
    flags = session_cookie(response).lower()
    assert "httponly" in flags and "secure" in flags and "samesite=strict" in flags and "path=/" in flags
    out = client.post("/api/logout", headers={"X-CSRF-Token": response.json()["csrf"], "X-Forwarded-Proto": "https"})
    cleared = session_cookie(out).lower()
    assert "max-age=0" in cleared and "httponly" in cleared and "secure" in cleared and "samesite=strict" in cleared


def test_cookie_is_secure_by_default_and_by_forwarded_https_only_from_trusted_peer(tmp_path):
    settings, app = make(tmp_path, secure_cookie=True)  # the env default may be off in a test run
    with TestClient(app, base_url="http://localhost") as client:
        create_owner(client, settings)
        assert "secure" in session_cookie(client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET})).lower()
    settings, app = make(tmp_path / "off", secure_cookie=False)  # "testclient" is not a trusted proxy here
    with TestClient(app, base_url="http://localhost") as client:
        create_owner(client, settings)
        forged = client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET},
                             headers={"X-Forwarded-Proto": "https"})
        assert "secure" not in session_cookie(forged).lower()


def test_login_throttle_uses_forwarded_client_ip_from_trusted_peer(tunnel):
    client, app, settings = tunnel
    create_owner(client, settings)
    attacker = {"CF-Connecting-IP": "203.0.113.7"}
    for _ in range(10):
        assert client.post("/api/login", json={"username": "owner", "password": "wrong"}, headers=attacker).status_code == 401
    assert client.post("/api/login", json={"username": "owner", "password": "wrong"}, headers=attacker).status_code == 429
    assert attempts(app) == {"203.0.113.7": 10}
    # Another real visitor behind the same tunnel is not locked out.
    other = client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET},
                        headers={"CF-Connecting-IP": "198.51.100.9"})
    assert other.status_code == 200
    xff = client.post("/api/login", json={"username": "owner", "password": "wrong"},
                      headers={"X-Forwarded-For": "6.6.6.6, 192.0.2.44"})
    assert xff.status_code == 401 and "192.0.2.44" in attempts(app) and "6.6.6.6" not in attempts(app)


def test_forwarding_headers_are_ignored_from_untrusted_peer(tmp_path):
    settings, app = make(tmp_path, secure_cookie=False)
    with TestClient(app, base_url="http://localhost") as client:
        create_owner(client, settings)
        spoof = {"CF-Connecting-IP": "203.0.113.7", "X-Forwarded-For": "203.0.113.8"}
        for _ in range(10):
            client.post("/api/login", json={"username": "owner", "password": "wrong"}, headers=spoof)
        limited = client.post("/api/login", json={"username": "owner", "password": "wrong"},
                              headers={"CF-Connecting-IP": "203.0.113.99"})
        assert limited.status_code == 429  # rotating the spoofed header changes nothing
        assert app.state.db.one("SELECT count(*) AS n FROM login_attempts WHERE ip='testclient'")["n"] == 10


class Headers(dict):
    """Lower-cased headers; a list value stands for several header lines (Starlette's getlist)."""
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]

    def get(self, key, default=None):
        value = super().get(key, default)
        return ",".join(value) if isinstance(value, list) else value


class Fake:
    def __init__(self, peer, headers=None, scheme="http"):
        from types import SimpleNamespace
        self.client = SimpleNamespace(host=peer) if peer else None
        self.headers = Headers({k.lower(): v for k, v in (headers or {}).items()})
        self.url = SimpleNamespace(scheme=scheme)


def test_client_ip_resolution_rules():
    trusted = parse_trusted(["127.0.0.1", "::1", "10.0.0.0/8"])
    assert client_ip(Fake("127.0.0.1"), trusted) == "127.0.0.1"
    assert client_ip(Fake("127.0.0.1", {"CF-Connecting-IP": "2001:db8::1"}), trusted) == "2001:db8::1"
    assert client_ip(Fake("::1", {"CF-Connecting-IP": "not-an-ip", "X-Forwarded-For": "1.2.3.4"}), trusted) == "1.2.3.4"
    assert client_ip(Fake("127.0.0.1", {"X-Forwarded-For": "9.9.9.9, 1.2.3.4, 10.1.1.1"}), trusted) == "1.2.3.4"
    assert client_ip(Fake("127.0.0.1", {"X-Forwarded-For": "garbage"}), trusted) == "127.0.0.1"
    assert client_ip(Fake("127.0.0.1", {"X-Forwarded-For": ["8.8.8.8", "100.64.7.9"]}), trusted) == "100.64.7.9"  # two header lines: right-most wins
    assert client_ip(Fake("10.2.3.4", {"CF-Connecting-IP": "1.2.3.4"}), trusted) == "1.2.3.4"
    assert client_ip(Fake("192.0.2.1", {"CF-Connecting-IP": "1.2.3.4"}), trusted) == "192.0.2.1"
    assert client_ip(Fake(None), trusted) == "unknown"
    assert is_https(Fake("127.0.0.1", {"X-Forwarded-Proto": "https"}), trusted)
    assert not is_https(Fake("192.0.2.1", {"X-Forwarded-Proto": "https"}), trusted)
    assert is_https(Fake("192.0.2.1", scheme="https"), trusted)


@pytest.mark.smoke
def test_setup_is_closed_once_the_owner_exists_and_uses_no_throttle_slot(tunnel):
    client, app, settings = tunnel
    create_owner(client, settings)
    body = {"username": "attacker", "password": OWNER_SECRET, "token": "x" * 40}
    before = app.state.db.one("SELECT count(*) AS n FROM login_attempts")["n"]
    for _ in range(15):
        assert client.post("/api/setup", json=body).status_code == 409
    assert app.state.db.one("SELECT count(*) AS n FROM login_attempts")["n"] == before
    assert app.state.db.one("SELECT username FROM owner")["username"] == "owner"
    assert client.post("/api/login", json={"username": "owner", "password": OWNER_SECRET}).status_code == 200


def test_setup_token_attempts_are_throttled_per_real_ip(tunnel):
    client, app, settings = tunnel
    body = {"username": "owner", "password": OWNER_SECRET, "token": "wrong-token-" + "x" * 30}
    guesser = {"CF-Connecting-IP": "203.0.113.50"}
    for _ in range(10):
        assert client.post("/api/setup", json=body, headers=guesser).status_code == 409
    assert client.post("/api/setup", json=body, headers=guesser).status_code == 429
    assert attempts(app) == {"203.0.113.50": 10}
    create_owner(client, settings, **{"CF-Connecting-IP": "198.51.100.1"})  # the owner is unaffected


def test_security_headers_and_hsts_only_over_https(tunnel):
    client, app, settings = tunnel
    for path in ("/", "/api/auth-status"):
        headers = client.get(path).headers
        assert headers["x-frame-options"] == "DENY"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cross-origin-opener-policy"] == "same-origin"
        assert "microphone=(self)" in headers["permissions-policy"] and "camera=()" in headers["permissions-policy"]
        assert "frame-ancestors 'none'" in headers["content-security-policy"]
        assert headers["strict-transport-security"].startswith("max-age=")
    _, plain_app = make(settings.data_dir.parent / "plain", secure_cookie=False)
    with TestClient(plain_app, base_url="http://localhost") as plain:
        assert "strict-transport-security" not in plain.get("/api/auth-status").headers

"""Add User over Tailscale only: the tailnet serve step, instance hosts, the manager URL, the enrollment report behind Tailscale
Serve and the guard that keeps Cloudflare steps off the production tunnel. Every external system is mocked."""
import json
import sqlite3
import subprocess

import pytest
from fastapi.testclient import TestClient
from test_onboarding import ENV, PC_DEVICE, Host
from test_provision import World, status, world  # noqa: F401  (the fixture)

from ai_manager import enrollment
from ai_manager import provision as pv

SELF = "manager.example.ts.net"


def titles(receipt):
    return [t for t, _, _ in receipt]


def flag_funnel(world, port):
    """After the serve command, Tailscale reports Funnel on for the port."""
    original = world.tailscale

    def tailscale(argv):
        result = original(argv)
        if argv[1:3] == ["serve", "--bg"]:
            world.serve["AllowFunnel"] = {f"{SELF}:{port}": True}
        return result
    world.tailscale = tailscale


# ---- tailnet-only is the default ---------------------------------------------------------------------------------------
@pytest.mark.smoke
def test_tailnet_only_preview_has_no_cloudflare_steps_and_includes_tailnet_serve(world):
    receipt = pv.provision(world.plan(False, domain=""))
    assert "tailnet serve" in titles(receipt)
    assert not [t for t in titles(receipt) if "cloudflare" in t]
    assert set(status(receipt).values()) == {"planned"}
    assert world.mutating() == []  # a preview changes nothing, `tailscale serve` included
    assert not [c for c in world.calls if c[0] in ("GET", "PUT", "POST") and "cloudflare" in c[1]]  # no Cloudflare traffic at all
    serve = dict((t, d) for t, _, d in receipt)["tailnet serve"]
    assert f"https://{SELF}:8788" in serve and "no Funnel" in serve


def test_a_domain_is_never_filled_in_by_the_screen_or_the_api(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, env={**ENV, "LINKO_DOMAIN": "example.com"})  # even a leftover LINKO_DOMAIN
    with h.client:
        h.sign_in()
        listing = h.client.get("/api/onboarding").json()
        assert "domain" not in listing
        record = h.create()  # no domain field at all
        assert record["domain"] == "" and record["hostname"] == ""
        h.tick()
        steps = [s["title"] for s in h.get(record["id"])["steps"]]
        assert "tailnet serve" in steps and not [t for t in steps if "cloudflare" in t]
        assert h.get(record["id"])["url"] == f"https://{SELF}:8788"
        assert not [c for c in h.world.calls if c[0] in ("GET", "PUT", "POST") and "cloudflare" in c[1]]


def test_cli_without_domain_is_tailnet_only(tmp_path, capsys, monkeypatch):
    world = World(tmp_path)

    def fake_run(argv, **kwargs):  # nothing real runs: not even `tailscale status`
        code, output = world.run(argv)
        return subprocess.CompletedProcess(argv, code, output, "")
    monkeypatch.setattr(pv.subprocess, "run", fake_run)
    code = pv.main(["add-user", "--name", "anna", "--email", "a@b.co", "--windows-folder", "C:\\W", "--skip-syncthing",
                    "--home-base", str(tmp_path / "none"), "--config", str(tmp_path / "none.env")], environ={})
    out = capsys.readouterr().out
    assert code == 0 and "tailnet serve" in out and "cloudflare" not in out.lower().replace("no cloudflare", "")
    assert f"instance URL https://{SELF}:" in out and not [c for c in world.calls if c[0] != "run" or c[1][0] in ("useradd", "chown")]


# ---- the serve step ----------------------------------------------------------------------------------------------------
def test_serve_is_applied_once_and_the_second_run_skips_it(world):
    first = pv.provision(world.plan(True, domain=""))
    assert status(first)["tailnet serve"] == "created"
    serve = [c[1] for c in world.calls if c[0] == "run" and c[1][:2] == ["tailscale", "serve"] and "status" not in c[1]]
    assert serve == [["tailscale", "serve", "--bg", "--https=8788", "http://127.0.0.1:8788"]]
    assert dict((t, d) for t, _, d in first)["tailnet serve"].count(f"https://{SELF}:8788") == 1
    second = pv.provision(world.plan(True, domain=""))
    assert set(status(second).values()) == {"skipped"}
    assert status(second)["tailnet serve"] == "skipped"
    assert len([c for c in world.calls if c[0] == "run" and c[1][:2] == ["tailscale", "serve"] and "status" not in c[1]]) == 1
    assert "AllowFunnel" not in world.serve


def test_serve_to_another_target_refuses_the_whole_run_with_no_change(world):
    world.serve = {"TCP": {"8788": {"HTTPS": True}}, "Web": {f"{SELF}:8788": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:9999"}}}}}
    receipt = pv.provision(world.plan(True, domain=""))
    assert status(receipt)["tailnet serve"] == "refused" and "not run" in status(receipt).values()
    assert world.mutating() == [] and "anna" not in world.users


@pytest.mark.parametrize("served", [
    {"TCP": {"8788": {"TCPForward": "127.0.0.1:22"}}},
    {"TCP": {"8788": {"HTTPS": True}}, "Web": {f"{SELF}:8788": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8788"}, "/x": {"Proxy": "http://127.0.0.1:1"}}}}},
    {"TCP": {"8788": {"HTTPS": True}}, "Web": {f"{SELF}:8788": {"Handlers": {"/": {"Path": "/srv"}}}}},
])
def test_other_serve_shapes_on_the_port_are_conflicts(world, served):
    world.serve = served
    assert status(pv.provision(world.plan(True, domain="")))["tailnet serve"] == "refused"
    assert world.mutating() == []


def test_funnel_is_never_used_and_refused_when_on(world):
    world.serve = {"TCP": {"8788": {"HTTPS": True}}, "Web": {f"{SELF}:8788": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8788"}}}},
                   "AllowFunnel": {f"{SELF}:8788": True}}
    receipt = pv.provision(world.plan(True, domain=""))
    assert status(receipt)["tailnet serve"] == "refused" and "Funnel" in dict((t, d) for t, _, d in receipt)["tailnet serve"]
    assert world.mutating() == []
    # a Funnel on any host for that port counts, and a Funnel flag that is off does not
    world.serve = {"AllowFunnel": {"other.ts.net:8788": True}}
    assert status(pv.provision(world.plan(False, domain="")))["tailnet serve"] == "refused"
    world.serve = {"AllowFunnel": {f"{SELF}:8788": False}}
    assert status(pv.provision(world.plan(False, domain="")))["tailnet serve"] == "planned"


def test_apply_asserts_the_result_has_no_funnel(world):
    flag_funnel(world, 8788)
    receipt = pv.provision(world.plan(True, domain=""))
    row = next(r for r in receipt if r[0] == "tailnet serve")
    assert row[1] == "failed" and "Funnel" in row[2]
    commands = [c[1] for c in world.calls if c[0] == "run" and c[1][0] == "tailscale"]
    assert not [c for c in commands if "funnel" in c]  # `tailscale funnel` is never run


def test_unreachable_tailscale_or_unknown_hostname_refuses(world):
    world.tailscale = lambda argv: (1, "tailscaled is not running")
    receipt = pv.provision(world.plan(False, domain=""))
    assert status(receipt)["tailnet serve"] == "refused"
    assert dict((t, d) for t, _, d in receipt)["tailnet serve"].startswith(("cannot determine the tailnet hostname", "tailscale serve status failed"))


# ---- the instance env file ---------------------------------------------------------------------------------------------
def test_env_file_hosts_for_tailnet_only_and_for_a_domain(world):
    plan = world.plan(True, domain="")
    pv.provision(plan)
    assert f"AI_MANAGER_ALLOWED_HOSTS=localhost,127.0.0.1,{SELF}\n" in plan.env_file.read_text()
    assert "anna." not in plan.env_file.read_text().replace("anna/", "").replace("anna:", "")
    other = world.plan(True, domain="example.com", name="bob")
    pv.provision(other)
    assert "AI_MANAGER_ALLOWED_HOSTS=localhost,127.0.0.1,bob.example.com\n" in other.env_file.read_text()
    assert SELF not in other.env_file.read_text()


def test_tailnet_host_comes_from_the_manager_url_without_its_port_else_from_tailscale(world):
    assert pv.tailnet_host({"LINKO_MANAGER_URL": "https://codex-server.tail5ebb9d.ts.net:8787"}, world.run) == "codex-server.tail5ebb9d.ts.net"
    assert pv.tailnet_host({}, world.run) == SELF
    plan = world.plan(False, domain="", config={**world.plan(False).config, "LINKO_MANAGER_URL": "https://m.tail1.ts.net:8787/"})
    assert plan.tailnet_host() == "m.tail1.ts.net"
    with pytest.raises(pv.ProvisionError, match="LINKO_MANAGER_URL"):
        pv.tailnet_host({}, lambda argv: (1, ""))


# ---- manager_url -------------------------------------------------------------------------------------------------------
class Tailscale:
    def __init__(self, served, name="codex-server.tail5ebb9d.ts.net."):
        self.served, self.name, self.calls = served, name, []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        out = json.dumps({"Self": {"DNSName": self.name}} if argv[1] == "status" else self.served)
        return subprocess.CompletedProcess(argv, 0, out, "")


def web(key, target):
    return {"Web": {key: {"Handlers": {"/": {"Proxy": target}}}}}


def test_manager_url_honours_an_explicit_port():
    run = Tailscale({})
    assert enrollment.manager_url({"LINKO_MANAGER_URL": "https://codex-server.tail5ebb9d.ts.net:8787/"}, run) \
        == "https://codex-server.tail5ebb9d.ts.net:8787"
    assert enrollment.manager_url({"LINKO_MANAGER_URL": "https://m.ts.net"}, run) == "https://m.ts.net"
    assert run.calls == []  # an explicit URL never asks Tailscale
    with pytest.raises(pv.ProvisionError):
        enrollment.manager_url({"LINKO_MANAGER_URL": "https://m.ts.net:8787/path"}, run)


def test_manager_url_fallback_detects_the_served_port_and_never_assumes_443():
    name = "codex-server.tail5ebb9d.ts.net"
    assert enrollment.manager_url({}, Tailscale(web(f"{name}:8787", "http://127.0.0.1:8787"))) == f"https://{name}:8787"
    assert enrollment.manager_url({}, Tailscale(web(f"{name}:443", "http://127.0.0.1:8787"))) == f"https://{name}"
    assert enrollment.manager_url({}, Tailscale(web(f"{name}:8443", "http://localhost:9000")), 9000) == f"https://{name}:8443"


@pytest.mark.parametrize("served", [
    {},  # nothing served
    web("codex-server.tail5ebb9d.ts.net:8787", "http://127.0.0.1:9999"),  # served, but not the manager
    web("other.ts.net:8787", "http://127.0.0.1:8787"),  # another node's name
    {"Web": {"codex-server.tail5ebb9d.ts.net:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}},
             "codex-server.tail5ebb9d.ts.net:8787": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8787"}}}}},  # ambiguous
])
def test_manager_url_fallback_fails_clearly_instead_of_guessing(served):
    with pytest.raises(pv.ProvisionError, match="LINKO_MANAGER_URL"):
        enrollment.manager_url({}, Tailscale(served))


def test_manager_url_fails_clearly_when_tailscale_is_unavailable():
    def broken(argv, **kwargs):
        raise FileNotFoundError("tailscale")
    with pytest.raises(pv.ProvisionError, match="LINKO_MANAGER_URL"):
        enrollment.manager_url({}, broken)


# ---- /api/enroll/report behind Tailscale Serve -------------------------------------------------------------------------
LOOPBACK = ("127.0.0.1", "::1")


@pytest.fixture
def serve_host(tmp_path, monkeypatch):
    """Trusted proxies = loopback only (Tailscale Serve's peer); the manager listens on 80 like the TestClient's server."""
    h = Host(tmp_path, monkeypatch, trusted=LOOPBACK)
    with h.client:
        h.sign_in()
        yield h


def via(host, peer, headers=None):
    """The enrolled PC's report arriving from `peer` (the direct TCP peer of the manager)."""
    record = host.create()
    host.tick()
    token = host.token_of(host.command(record["id"])["command"])
    client = TestClient(host.app, base_url="http://localhost", client=(peer, 50000))
    body = {"token": token, "hostname": "ANNA-PC", "tailscale_ip": "100.101.102.103", "device_id": PC_DEVICE}
    return client.post("/api/enroll/report", json=body, headers=headers or {}), record


def test_loopback_serve_with_a_tailnet_forwarded_address_is_accepted(serve_host):
    response, record = via(serve_host, "127.0.0.1", {"x-forwarded-for": "100.64.7.9", "tailscale-user-login": "anna@example.com"})
    assert response.status_code == 200 and serve_host.get(record["id"])["device_id"] == PC_DEVICE


def test_a_forged_forwarded_for_from_an_untrusted_peer_is_ignored(serve_host):
    identity = {"tailscale-user-login": "anna@example.com", "tailscale-user-name": "Anna"}  # no authentication value either
    for headers in ({"x-forwarded-for": "100.64.7.9"}, {"x-forwarded-for": "100.64.7.9", **identity}, identity):
        response, record = via(serve_host, "203.0.113.5", headers)
        assert response.status_code == 403 and serve_host.get(record["id"])["device_id"] == ""
        with sqlite3.connect(serve_host.settings.db_path) as conn:
            conn.execute("DELETE FROM onboarding")


def test_loopback_with_a_non_tailnet_forwarded_address_is_rejected(serve_host):
    for forwarded in ("8.8.8.8", "100.64.7.9, 8.8.8.8", "192.168.1.5", "", "garbage"):
        headers = {"x-forwarded-for": forwarded} if forwarded else {}
        response, _ = via(serve_host, "127.0.0.1", headers)
        assert response.status_code == 403, forwarded
        with sqlite3.connect(serve_host.settings.db_path) as conn:
            conn.execute("DELETE FROM onboarding")


def test_the_right_most_untrusted_hop_decides_not_the_left_most(serve_host):
    # a tailnet client prepends a lie; Serve appends the address it really saw
    response, _ = via(serve_host, "127.0.0.1", {"x-forwarded-for": "8.8.8.8, 100.64.7.9"})
    assert response.status_code == 200


def test_a_direct_non_tailnet_peer_is_rejected_and_a_direct_tailnet_peer_needs_no_header(serve_host):
    response, _ = via(serve_host, "198.51.100.7")
    assert response.status_code == 403
    with sqlite3.connect(serve_host.settings.db_path) as conn:
        conn.execute("DELETE FROM onboarding")
    response, _ = via(serve_host, "100.64.0.9")
    assert response.status_code == 200


def test_cloudflare_headers_are_not_believed_on_the_report_route(serve_host):
    for extra in ({"cf-connecting-ip": "100.64.7.9"}, {"cf-ray": "abc"}):
        response, _ = via(serve_host, "127.0.0.1", {"x-forwarded-for": "100.64.7.9", **extra})
        assert response.status_code == 403
        with sqlite3.connect(serve_host.settings.db_path) as conn:
            conn.execute("DELETE FROM onboarding")


def test_the_report_must_arrive_on_the_managers_own_listener(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, trusted=LOOPBACK, port=8787)  # the TestClient's server port is 80: another listener
    with h.client:
        h.sign_in()
        response, record = via(h, "127.0.0.1", {"x-forwarded-for": "100.64.7.9"})
        assert response.status_code == 403 and h.get(record["id"])["device_id"] == ""


def test_the_enrollment_token_stays_the_only_credential(serve_host):
    record = serve_host.create()
    serve_host.tick()
    serve_host.command(record["id"])
    client = TestClient(serve_host.app, base_url="http://localhost", client=("127.0.0.1", 1))
    body = {"token": "x" * 40, "hostname": "ANNA-PC", "tailscale_ip": "100.101.102.103", "device_id": PC_DEVICE}
    headers = {"x-forwarded-for": "100.64.7.9", "tailscale-user-login": "owner@example.com", "tailscale-user-name": "Owner"}
    assert client.post("/api/enroll/report", json=body, headers=headers).status_code == 403  # a tailnet identity is no token
    assert serve_host.get(record["id"])["device_id"] == ""


# ---- the production-tunnel guard ---------------------------------------------------------------------------------------
def writes(world):
    return [c for c in world.calls if c[0] in ("PUT", "POST") and "rest" not in c[1]]


def test_a_tunnel_with_foreign_hostnames_gets_no_write(world):
    world.ingress = [{"hostname": "messenger.example.com", "service": "http://127.0.0.1:8787"}, {"service": "http_status:404"}]
    receipt = pv.provision(world.plan(True))
    assert status(receipt)["cloudflare tunnel route"] == "refused"
    assert "messenger.example.com" in dict((t, d) for t, _, d in receipt)["cloudflare tunnel route"]
    assert writes(world) == [] and world.mutating() == [] and "anna" not in world.users
    assert world.ingress[0]["hostname"] == "messenger.example.com" and len(world.ingress) == 2


def test_the_production_tunnel_is_refused_by_name_before_any_write(world):
    world.tunnel_name = "linko-messenger-webhook"
    for config in ({}, {"LINKO_ONBOARDING_HOSTNAMES": "messenger.example.com"}):  # empty ingress and an allow-list change nothing
        receipt = pv.provision(world.plan(True, config={**world.plan(True).config, **config}))
        assert set(status(receipt).values()) <= {"refused", "not run"} and status(receipt)["cloudflare tunnel route"] == "refused"
        assert "linko-messenger-webhook" in dict((t, d) for t, _, d in receipt)["cloudflare tunnel route"]
    world.ingress = [{"hostname": "anna.example.com", "service": "http://127.0.0.1:8788"}, {"service": "http_status:404"}]
    assert status(pv.provision(world.plan(True)))["cloudflare dns cname"] == "refused"  # every Cloudflare step is guarded
    assert writes(world) == [] and world.mutating() == []


def test_a_tunnel_not_named_for_onboarding_is_refused(world):
    world.tunnel_name = "some-other-tunnel"
    receipt = pv.provision(world.plan(True))
    assert status(receipt)["cloudflare tunnel route"] == "refused" and "dedicated" in dict((t, d) for t, _, d in receipt)["cloudflare tunnel route"]
    assert writes(world) == []


def test_a_dedicated_tunnel_accepts_its_own_hostnames_records_and_the_allow_list(world, tmp_path):
    db = tmp_path / "manager.sqlite3"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE onboarding(name TEXT, domain TEXT)")
        conn.executemany("INSERT INTO onboarding VALUES(?,?)", [("carl", "example.com"), ("dora", "")])
    assert pv.known_hostnames(tmp_path) == frozenset({"carl.example.com"}) and pv.known_hostnames(tmp_path / "missing") == frozenset()
    world.ingress = [{"hostname": "carl.example.com", "service": "http://127.0.0.1:8789"},
                     {"hostname": "Extra.example.com", "service": "http://127.0.0.1:8790"}, {"service": "http_status:404"}]
    config = {**world.plan(True).config, "LINKO_ONBOARDING_HOSTNAMES": "extra.example.com, x.example.com"}
    receipt = pv.provision(world.plan(True, config=config, onboarding_hostnames=pv.known_hostnames(tmp_path)))
    assert set(status(receipt).values()) <= {"created", "allocated", "started"}
    assert [r.get("hostname") for r in world.ingress] == ["carl.example.com", "Extra.example.com", "anna.example.com", None]  # catch-all last
    # without those records/allow-list the same tunnel is refused
    world2 = World(tmp_path / "second")
    world2.tmp.mkdir()
    world2.ingress = list(world.ingress[:2]) + [{"service": "http_status:404"}]
    assert status(pv.provision(world2.plan(True)))["cloudflare tunnel route"] == "refused" and writes(world2) == []

"""Add User isolation: the app-level client allow-list (AI_MANAGER_ALLOWED_CLIENTS), the provisioning step that writes it, the
per-user Tailscale tag and the generated (never applied) policy snippet. Every external system is mocked."""
import json
import secrets

import pytest
from fake_codex import FakeServer
from fastapi.testclient import TestClient
from test_onboarding import ENV, FORM, PC_DEVICE, Host
from test_provision import World, status, world  # noqa: F401  (the fixture)

from ai_manager import provision as pv
from ai_manager import tailnet_policy
from ai_manager.config import Settings
from ai_manager.web import create_app

PC, OTHER = "100.64.0.5", "100.64.0.9"
LOOPBACK = ("127.0.0.1", "::1")


def instance(tmp_path, allowed=(PC,), **over):
    (tmp_path / "projects").mkdir(exist_ok=True)
    settings = Settings(data_dir=tmp_path / "data", project_roots=(tmp_path / "projects",), secure_cookie=False,
                        trusted_proxies=LOOPBACK, allowed_clients=allowed, **over)
    return create_app(settings, FakeServer(settings))


def get(app, peer, headers=None, path="/api/auth-status", method="GET"):
    return TestClient(app, base_url="http://localhost", client=(peer, 50000)).request(method, path, headers=headers or {})


@pytest.fixture
def app(tmp_path):
    return instance(tmp_path)


# ---- layer 1: the allow-list -------------------------------------------------------------------------------------------
@pytest.mark.smoke
def test_own_pc_through_serve_is_allowed_and_another_tailnet_ip_gets_403(app):
    assert get(app, "127.0.0.1", {"x-forwarded-for": PC}).status_code == 200
    for path in ("/api/auth-status", "/", "/api/projects"):  # 403 before auth and before any route
        response = get(app, "127.0.0.1", {"x-forwarded-for": OTHER}, path)
        assert response.status_code == 403 and response.json() == {"detail": "Forbidden"}
    assert get(app, "127.0.0.1", {"x-forwarded-for": OTHER}, "/api/login", "POST").status_code == 403
    assert get(app, "127.0.0.1", {"x-forwarded-for": OTHER}, "/api/enroll/report", "POST").status_code == 403


def test_the_check_runs_before_the_host_check(app):
    bad_host = {"x-forwarded-for": OTHER, "host": "evil.example"}
    assert get(app, "127.0.0.1", bad_host).status_code == 403  # not the host check's 400
    assert get(app, "127.0.0.1", {"x-forwarded-for": PC, "host": "evil.example"}).status_code == 400


def test_a_forged_forwarded_for_from_a_non_loopback_peer_is_ignored(app):
    assert get(app, OTHER, {"x-forwarded-for": PC}).status_code == 403  # another device claiming to be the PC
    assert get(app, "203.0.113.7", {"x-forwarded-for": PC}).status_code == 403
    assert get(app, PC).status_code == 200  # a direct connection is judged by its own address
    assert get(app, PC, {"x-forwarded-for": OTHER}).status_code == 200  # ...and its headers change nothing either way


def test_the_right_most_hop_decides(app):
    assert get(app, "127.0.0.1", {"x-forwarded-for": f"{OTHER}, {PC}"}).status_code == 200  # Serve appended the real address
    assert get(app, "127.0.0.1", {"x-forwarded-for": f"{PC}, {OTHER}"}).status_code == 403  # the PC address is only a forgery
    assert get(app, "127.0.0.1", {"x-forwarded-for": f"{PC}, 127.0.0.1, {OTHER}"}).status_code == 403


def test_cloudflare_and_unparsable_headers_never_let_a_client_in(app):
    forged = {"x-forwarded-for": OTHER, "cf-connecting-ip": PC, "cf-ray": "abc"}
    assert get(app, "127.0.0.1", forged).status_code == 403
    for value in ("garbage", f"{PC}, garbage", "unknown", f"{PC},"):
        assert get(app, "127.0.0.1", {"x-forwarded-for": value}).status_code == 403, value


def test_loopback_health_checks_are_allowed(app):
    assert get(app, "127.0.0.1").status_code == 200 and get(app, "::1").status_code == 200
    assert get(app, "127.0.0.1", {"x-forwarded-for": "127.0.0.1"}).status_code == 200


def test_unset_keeps_the_old_behaviour(tmp_path):
    open_app = instance(tmp_path, allowed=())
    for peer, headers in (("127.0.0.1", {"x-forwarded-for": OTHER}), (OTHER, {}), ("203.0.113.7", {"x-forwarded-for": PC})):
        assert get(open_app, peer, headers).status_code == 200


def test_a_restricted_instance_enforces_it_before_auth(tmp_path):
    project = tmp_path / "work" / "folder"
    project.mkdir(parents=True)
    settings = Settings(data_dir=tmp_path / "work" / "data", project_roots=(tmp_path / "work",), restricted=True, project_path=project,
                        secure_cookie=False, trusted_proxies=LOOPBACK, allowed_clients=(PC,))
    restricted = create_app(settings, FakeServer(settings))
    assert get(restricted, "127.0.0.1", {"x-forwarded-for": PC}).status_code == 200
    assert get(restricted, "127.0.0.1", {"x-forwarded-for": OTHER}).status_code == 403


def test_the_list_takes_cidrs_ipv4_mapped_addresses_and_rejects_bad_entries(tmp_path):
    cidr = instance(tmp_path, allowed=("100.64.1.0/24", "fd7a:115c:a1e0::5"))
    assert get(cidr, "127.0.0.1", {"x-forwarded-for": "100.64.1.77"}).status_code == 200
    assert get(cidr, "127.0.0.1", {"x-forwarded-for": "fd7a:115c:a1e0::5"}).status_code == 200
    assert get(cidr, "127.0.0.1", {"x-forwarded-for": "::ffff:100.64.1.77"}).status_code == 200
    assert get(cidr, "127.0.0.1", {"x-forwarded-for": "100.64.2.1"}).status_code == 403
    with pytest.raises(ValueError, match="ALLOWED_CLIENTS"):
        instance(tmp_path, allowed=("not-an-ip",))  # fails closed at start, never silently open


def test_the_setting_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("AI_MANAGER_ALLOWED_CLIENTS", f" {PC} , ,100.64.1.0/24")
    assert Settings().allowed_clients == (PC, "100.64.1.0/24")
    monkeypatch.delenv("AI_MANAGER_ALLOWED_CLIENTS")
    assert Settings().allowed_clients == ()


# ---- the provisioning step ---------------------------------------------------------------------------------------------
def env_lines(plan):
    return plan.env_file.read_text().splitlines()


def test_a_new_env_file_carries_the_pcs_address_and_a_rerun_is_a_no_op(world):
    plan = world.plan(True, domain="", client_ip=PC)
    first = pv.provision(plan)
    assert [line for line in env_lines(plan) if "ALLOWED_CLIENTS" in line] == [f"AI_MANAGER_ALLOWED_CLIENTS={PC}"]
    assert "client allow-list" in status(first)
    before = plan.env_file.read_text()
    second = pv.provision(world.plan(True, domain="", client_ip=PC))
    assert set(status(second).values()) == {"skipped"} and plan.env_file.read_text() == before


def test_an_existing_env_file_gets_the_address_once_and_the_instance_restarts(world):
    plan = world.plan(True, domain="")
    pv.provision(plan)  # an instance created before the PC reported in
    assert not [line for line in env_lines(plan) if "ALLOWED_CLIENTS" in line]
    world.calls.clear()
    receipt = pv.provision(world.plan(True, domain="", client_ip=PC))
    assert status(receipt)["client allow-list"] == "created"
    assert [line for line in env_lines(plan) if "ALLOWED_CLIENTS" in line] == [f"AI_MANAGER_ALLOWED_CLIENTS={PC}"]
    assert ["systemctl", "try-restart", plan.unit_name] in [c[1] for c in world.calls if c[0] == "run"]
    again = pv.provision(world.plan(True, domain="", client_ip=PC))
    assert status(again)["client allow-list"] == "skipped" and len([x for x in env_lines(plan) if "ALLOWED_CLIENTS" in x]) == 1


def test_a_different_existing_value_is_refused_and_nothing_changes(world):
    plan = world.plan(True, domain="", client_ip=PC)
    pv.provision(plan)
    before = plan.env_file.read_text()
    world.calls.clear()
    receipt = pv.provision(world.plan(True, domain="", client_ip=OTHER))
    assert status(receipt)["client allow-list"] == "refused" and "not overwriting" in dict((t, d) for t, _, d in receipt)["client allow-list"]
    assert plan.env_file.read_text() == before and world.mutating() == []


def test_no_address_means_no_step_and_a_non_tailnet_address_is_refused(world):
    plan = world.plan(True, domain="")
    assert "client allow-list" not in [t for t, _, _ in pv.provision(plan)]
    assert "ALLOWED_CLIENTS" not in plan.env_file.read_text()
    receipt = pv.provision(world.plan(False, domain="", name="bob", client_ip="8.8.8.8"))
    assert status(receipt)["client allow-list"] == "refused"


def test_the_pcs_report_flows_into_the_instance_env_file(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, env={**ENV})
    with h.client:
        h.sign_in()
        record = h.create()
        h.tick()
        token = h.token_of(h.command(record["id"])["command"])
        assert h.report(token).status_code == 200  # reports 100.101.102.103
        h.client.post(f"/api/onboarding/{record['id']}/apply")
        h.tick()
        done = h.get(record["id"])
        assert done["state"] == "done", done["error"]
        env = (tmp_path / "home/anna/.config/linko-bot-manager-anna.env").read_text().splitlines()
        assert "AI_MANAGER_ALLOWED_CLIENTS=100.101.102.103" in env


def test_the_root_helper_refuses_a_forged_address_in_a_spooled_request(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    with h.client:
        h.sign_in()
        record = h.create()
        request = h.settings.data_dir / "onboarding/requests" / f"{record['id']}.json"
        data = json.loads(request.read_text())
        request.write_text(json.dumps({**data, "tailscale_ip": "8.8.8.8\nAI_MANAGER_RESTRICTED=0"}))
        with pytest.raises(pv.ProvisionError, match="malformed"):
            pv.handle_request(request, json.loads(h.host_config.read_text()), h.settings.data_dir / "onboarding/results",
                              run=h.world.run, http=h.world.http)


# ---- layer 2: per-user tag and policy snippet --------------------------------------------------------------------------
@pytest.mark.smoke
def test_snippet_grants_each_tag_only_its_port_and_8787_and_the_manager_only_ssh_and_syncthing():
    text = tailnet_policy.snippet([("anna", 8788), ("bob", 8790)], "100.100.0.1")
    data = json.loads(text)
    assert set(data) == {"hosts", "tagOwners", "grants"}  # nothing else
    assert data["hosts"] == {"linko-manager": "100.100.0.1"}
    assert data["tagOwners"] == {"tag:linko-anna": ["autogroup:admin"], "tag:linko-bob": ["autogroup:admin"]}
    assert data["grants"] == [
        {"src": ["tag:linko-anna"], "dst": ["linko-manager"], "ip": ["tcp:8788", "tcp:8787"]},
        {"src": ["linko-manager"], "dst": ["tag:linko-anna"], "ip": ["tcp:22", "tcp:22000"]},
        {"src": ["tag:linko-bob"], "dst": ["linko-manager"], "ip": ["tcp:8790", "tcp:8787"]},
        {"src": ["linko-manager"], "dst": ["tag:linko-bob"], "ip": ["tcp:22", "tcp:22000"]}]
    for grant in data["grants"]:  # no wildcard, no shared tag, no other user's port
        assert "*" not in json.dumps(grant) and "tag:linko-pc" not in json.dumps(grant) and set(grant) == {"src", "dst", "ip"}
    assert "8790" not in json.dumps(data["grants"][0]) and "8788" not in json.dumps(data["grants"][2])


def test_unknown_manager_ip_leaves_a_visible_placeholder():
    assert json.loads(tailnet_policy.snippet([("anna", 8788)]))["hosts"] == {"linko-manager": tailnet_policy.UNKNOWN_IP}


def test_the_minted_key_carries_the_users_own_tag_not_the_shared_one(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch, env={**ENV, "TAILSCALE_TAG": "tag:linko-pc"})  # a leftover shared tag is not used
    with h.client:
        h.sign_in()
        h.command(h.create()["id"])
        assert h.tailscale[-1][3]["capabilities"]["devices"]["create"]["tags"] == ["tag:linko-anna"]
        h.command(h.create(name="bob", email="bob@example.com")["id"])
        assert h.tailscale[-1][3]["capabilities"]["devices"]["create"]["tags"] == ["tag:linko-bob"]


def test_the_record_carries_the_tag_and_its_snippet_once_the_port_is_known(host_with_port):
    h, record = host_with_port
    entry = h.get(record["id"])
    assert entry["tag"] == "tag:linko-anna"
    data = json.loads(entry["policy"])
    assert data["hosts"] == {"linko-manager": "100.100.0.1"}
    assert data["grants"][0] == {"src": ["tag:linko-anna"], "dst": ["linko-manager"], "ip": ["tcp:8788", "tcp:8787"]}
    assert data["tagOwners"] == {"tag:linko-anna": ["autogroup:admin"]}


def test_no_snippet_before_the_port_is_allocated_and_the_policy_is_never_applied(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    with h.client:
        h.sign_in()
        record = h.create()  # the host helper has not answered yet: no port
        assert h.get(record["id"])["policy"] is None and h.get(record["id"])["tag"] == "tag:linko-anna"
        h.tick()
        h.command(record["id"])
        # the only Tailscale API traffic is the key mint: never a policy/acl call
        assert [c[1] for c in h.tailscale if "/acl" in c[1] or "policy" in c[1]] == []
        assert all(c[1].endswith("/keys") for c in h.tailscale)


def test_the_receipt_screen_shows_the_snippet():
    js = (pv.Path(__file__).resolve().parents[1] / "ai_manager/static/onboarding.js").read_text()
    assert "renderOnboardingPolicy(entry,body)" in js and "entry.policy" in js and "entry.tag" in js


@pytest.fixture
def host_with_port(tmp_path, monkeypatch):
    h = Host(tmp_path, monkeypatch)
    with h.client:
        h.sign_in()
        record = h.create()
        h.tick()
        yield h, record

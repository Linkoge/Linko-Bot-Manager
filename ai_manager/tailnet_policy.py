"""Network-level isolation for Add User: the Tailscale policy snippet that matches the per-user tags. Generated, never applied.

Each onboarded PC joins with its own tag, `tag:linko-<name>` (ai_manager/enrollment.py mints the auth key with it). The
snippet gives that tag exactly: the manager host on tcp:<its instance port> and tcp:8787 (the PC's one report), and lets the
manager reach the tag on 22 (SSH) and 22000 (Syncthing). Nothing else. The owner pastes it into the tailnet policy file;
this code makes no Tailscale API call. Standard library only. Runbook: docs/ONBOARDING.md ("Isolation").
"""
import json

MANAGER_PORT = 8787
MANAGER_ALIAS = "linko-manager"  # a `hosts` alias for the manager's tailnet IP: no tag is put on the manager itself
MANAGER_TO_PC = ("tcp:22", "tcp:22000")
TAG_OWNER = "autogroup:admin"
UNKNOWN_IP = "<MANAGER_TAILSCALE_IP>"


def user_tag(name):
    return f"tag:linko-{name}"


def policy(users, manager_ip=""):
    """The policy fragment for `users`, a list of (name, instance port). Pure data, ready for json.dumps."""
    users = sorted(users)
    grants = []
    for name, port in users:
        grants.append({"src": [user_tag(name)], "dst": [MANAGER_ALIAS], "ip": [f"tcp:{int(port)}", f"tcp:{MANAGER_PORT}"]})
        grants.append({"src": [MANAGER_ALIAS], "dst": [user_tag(name)], "ip": list(MANAGER_TO_PC)})
    return {"hosts": {MANAGER_ALIAS: manager_ip or UNKNOWN_IP},
            "tagOwners": {user_tag(name): [TAG_OWNER] for name, _ in users},
            "grants": grants}


def snippet(users, manager_ip=""):
    """The same fragment as text to paste into the tailnet policy file (valid JSON, hence valid HuJSON)."""
    return json.dumps(policy(users, manager_ip), indent=2)

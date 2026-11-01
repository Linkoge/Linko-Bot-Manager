# Onboarding a restricted user

The goal: fill three fields in the owner UI, paste one line on the new person's Windows PC, click Apply. Access is
**tailnet only** (Tailscale Serve): no Cloudflare account, tunnel, DNS record or domain is needed or touched. Cloudflare is an
optional appendix ([Appendix A](#appendix-a-optional-cloudflare-access-dedicated-tunnel-only)). This page is the
complete guide: where everything lives, the one-time setup, the daily flow, the access URL per user, the trusted-proxy
setting, [isolation](#32-isolation)(#32-isolation), the command-line fallback, troubleshooting, removal and rollback. The instance it creates is the existing restricted work instance
([INSTALL.md section 6](INSTALL.md#6-optional-extra-instances),
[README](../README.md#multiple-instances-and-the-restricted-work-instance)).

```
owner UI "Add user"  ──plan/apply──▶  spool  ──▶  user path unit ──sudo──▶  root-owned launcher (copy of provision.py)
        ▲                                                                       │ Linux user, instance, ACLs,
        │ device ID, hostname, IP  ◀── one report, one-time token ◀── Windows ◀─┘ tailnet serve, Syncthing
        └──── receipt, initial login (shown once)                      PC (one pasted line, over the tailnet)
```

## 1. Where everything lives

| What | Where | Owner / mode |
| --- | --- | --- |
| Add User screen, onboarding records (name, email, state, receipt) | project menu → **Add user**; table `onboarding` in the manager's `manager.sqlite3` | manager user |
| Credentials (all API keys) | `~/.config/linko-onboarding.env` of the manager user (`AI_MANAGER_ONBOARDING_CONFIG` overrides) | manager user, `chmod 600`, never in git |
| Management SSH key (public part is embedded in the PC command) | `~/.ssh/id_ed25519.pub` of the manager user (`LINKO_MANAGEMENT_PUBKEY_FILE` overrides) | manager user |
| Spool between the web app and the root helper | `<data>/onboarding/requests/`, `results/`, `installed.json` | manager user, `0700` |
| Root helper (copy of `ai_manager/provision.py`) | `/usr/local/lib/linko/onboarding-launcher.py` | root, `0755` |
| Root helper configuration | `/etc/linko/onboarding.json` (service user, data dir, repo, env file) | root, `0644` |
| Sudoers rule | `/etc/sudoers.d/linko-onboarding` | root, `0440` |
| Helper units | `~/.config/systemd/user/linko-bot-manager-onboarding.{path,service}` | manager user |
| Windows script (single source; also embedded in the PC command) | `deploy/windows/Linko-Setup.ps1` | repository |
| New user's instance | Linux user `<name>`, served on the tailnet at `https://<manager tailnet name>:<port>` (random free port 20000-29999, shown in the receipt): `/home/<name>/Documents/<name>` (the shared folder), `~/.config/linko-bot-manager-<name>.env`, `~/.local/share/linko-bot-manager-<name>/` (database, `initial-login.txt` until read), system unit `/etc/systemd/system/linko-bot-manager-<name>.service` | `<name>` |
| Deploy registration | `instances` list in `<data>/deploy/installed.json` | manager user |
| CLI fallback | `python -m ai_manager.provision add-user` | root |

## 2. One-time setup on the management server

### 2.1 Packages and prerequisites

- The manager already runs, deploys are installed (`python3 scripts/install_deploy.py`), and the root-owned launcher
  mechanism is available (`sudo`, `visudo`). Install ACL tools: `sudo apt install acl`.
- The manager is reachable **on the tailnet** (this is how the new PC reports in; the public Cloudflare hostname is
  deliberately refused for that route). With Tailscale Serve on the manager's own port (this host's example: 31847; not 443; the Cloudflare tunnel for production
  Messenger traffic is unrelated and never touched):
  `tailscale serve --bg --https=<manager port> http://127.0.0.1:<manager port>` (e.g. `--https=31847 http://127.0.0.1:31847`), and make sure the tailnet name is in
  `AI_MANAGER_ALLOWED_HOSTS` (the manager's own env file). Never enable Funnel for it.
- **Trusted proxy** ([section 3.1](#31-the-trusted-proxy-setting)): the manager must trust loopback as its proxy. That is
  already the default (`AI_MANAGER_TRUSTED_PROXIES=127.0.0.1,::1`); do not widen it.
- The Syncthing instance on the management server is running (GUI/API on `http://127.0.0.1:8384`) and is the same
  one that shares the manager's other folders.
- A management SSH key without passphrase: `ssh-keygen -t ed25519` as the manager user. Only the **public** key is ever
  copied to the PC.

### 2.2 Credentials file

Create `~/.config/linko-onboarding.env` (`chmod 600`, outside any git work tree, plain `KEY=VALUE`; environment variables
win over the file). The helper root process reads the same file for the Syncthing key (and the Cloudflare keys of the optional appendix).

| Key | Purpose |
| --- | --- |
| `SYNCTHING_API_KEY` | Local Syncthing API key |
| `SYNCTHING_URL` | Optional, default `http://127.0.0.1:8384` |
| `SYNCTHING_USER` | Optional: the Linux user the Syncthing daemon runs as (default: the manager user) |
| `TAILSCALE_API_KEY`, `TAILSCALE_TAILNET` | Mint single-use PC auth keys on demand (below). Without them the screen shows a field to paste one key |
| `LINKO_MANAGER_TAILNET_IP` | Optional: the manager's own Tailscale IPv4, used only in the generated policy snippet (default: `tailscale ip -4`; if unknown the snippet shows `<MANAGER_TAILSCALE_IP>` to fill in) |
| `LINKO_MANAGER_URL` | The manager's tailnet URL **with its port**, e.g. `https://codex-server.tail5ebb9d.ts.net:31847` (this host's example). Its port is **the manager port** used everywhere below (policy grant, port exclusion); without a port in the URL the app's `AI_MANAGER_PORT` setting is used. Its host is also the name every instance is reached under. Without it the host comes from `tailscale status --json` and the port from `tailscale serve status --json` (the port that serves `127.0.0.1:<manager port>`); if that cannot be determined the command fails and asks you to set this. 443 is never assumed |
| `LINKO_SYNCTHING_DEVICE_ID` | Optional: the management Syncthing's device ID (default: read through the API) |
| `LINKO_MANAGEMENT_PUBKEY_FILE` | Optional: another public key file |

**Tailscale API key.** Optional; without it the screen asks for a pasted single-use key. The exact credential, its
permissions and the policy to apply once are in [3.2 Isolation](#32-isolation). Each user's key is tagged `tag:linko-<name>`
(never a shared tag) and is created per click: single use (`reusable: false`), preauthorized, expiring after two hours.
The API key itself never leaves the server.

**Syncthing API key.** Syncthing GUI → *Actions → Settings → GUI → API Key*.

### 2.3 Install the root helper (once)

The web service runs as the normal manager user with `NoNewPrivileges`, so it can never run `sudo`. Like the emergency
repair path, it only writes a request file; a small user unit starts a root-owned launcher through a sudoers rule that
allows exactly three commands (`tick`, `service stop <name>`, `service start <name>` with this data directory).

```bash
sudo python3 scripts/install_onboarding.py install     # root part (add --dry-run to look first)
python3 scripts/install_onboarding.py activate         # as the manager user: spool + path unit
python3 scripts/install_onboarding.py status           # check
```

Re-run `install` after updating `ai_manager/provision.py` (the root copy is a copy; `status` shows `launcher_current`).
The first request is the proof: the Add User screen's plan preview only appears when the helper answers.

## 3. Adding a user (the daily flow)

1. Project menu → **Add user** (main instance only; restricted instances do not have the screen, and their API refuses it).
2. Fill **Name** (`anna`), **Email** (their app login) and **Windows folder**. *Advanced*: memory limit, and an optional
   Cloudflare domain that stays **empty** (nothing is pre-filled; an empty domain means tailnet only). Click **Preview plan**. Nothing has been changed yet: the
   host helper only checks and lists every step as `planned`, `skipped` (already there) or `refused` (conflict). The
   tailnet-only plan has a **tailnet serve** step and no Cloudflare step.
3. Click **Generate PC command**. A single-use Tailscale auth key is minted (or paste one if no API key is configured), and
   one PowerShell line appears. Copy it. It expires in two hours and is shown only now.
4. On the new PC: open **PowerShell as Administrator**, paste, Enter. It installs Tailscale, OpenSSH (key only, reachable
   only from the tailnet), and Syncthing; trusts the manager's Syncthing device and pre-creates the share with the right
   folder ID, so no Syncthing click is needed; then reports hostname, Tailscale IP and Syncthing device ID **once**.
5. The screen fills in the device ID by itself. Click **Apply**, or tick *Apply automatically when the PC reports in*
   before that and it continues alone.
6. Progress shows step by step. At the end **Show initial login (once)** reveals email and the generated password. Hand
   them over; the screen cannot show them again. `initial-login.txt` is already deleted from the server. The person logs
   in to their AI provider (Codex/Claude) themselves; nothing is copied from the owner. Tell them their address (next section).
7. If a step fails, the receipt names it and the record is `failed`. Fix the cause and click **Retry**: it re-runs the same
   idempotent command, everything done is skipped.

### Access URL per user

Port allocation: a random port in 20000-29999 that is not the manager's configured port (the port of `LINKO_MANAGER_URL`, else `AI_MANAGER_PORT`), not listening on 127.0.0.1 or 0.0.0.0, not in any
`linko-bot-manager*.env`, and not used by `tailscale serve status --json`. After 200 failed picks the **local port** step refuses
with "no free port found in 20000-29999". Troubleshooting: if the receipt shows that refusal, free ports on the host (`ss -ltn`)
or clear stale `tailscale serve` entries, then Retry. If a record's policy snippet changes after Apply, the saved port had become busy.

Each instance gets its own HTTPS port on the manager's tailnet name: **`https://<manager tailnet name>:<port>`**, e.g.
`https://codex-server.tail5ebb9d.ts.net:23417` (the port is picked at random from 20000-29999 and written in the receipt's
**tailnet serve** step and on the record). It only works from a device on the tailnet (the new PC joined it during
enrollment), signed in with the email and generated password. Nothing is reachable from the public internet.

States of a record: `pending` → `enrolling` (command generated) → `provisioning` → `done` or `failed` (Retry →
`provisioning`). The record, its latest plan or receipt and the PC's report survive reloads and restarts.

### What Apply does (each step checks first, then reconciles; existing things are never overwritten)

Linux user (no sudo, no password), project and data directories, `git init`, free localhost port, instance env file
(`AI_MANAGER_RESTRICTED=1`, `AI_MANAGER_ALLOWED_HOSTS=localhost,127.0.0.1,<manager tailnet name>`, no secrets), system unit with `MemoryMax`, the app login (random password), service enabled;
**ACLs**: the Syncthing daemon user gets read/write on the new folder only (plus search-only `--x` on its parent
directories), the new user gets default read/write on files Syncthing creates there, and the manager user gets access to the
instance's data directory (deploy snapshots and health checks); **client allow-list** (once the PC has reported in: its
Tailscale IP is written as `AI_MANAGER_ALLOWED_CLIENTS=<ip>` into the env file, idempotently, a different existing value is
refused; an already running instance is restarted, see [3.2](#32-isolation)); **tailnet serve**
(`tailscale serve --bg --https=<port> http://127.0.0.1:<port>`; checked first with `tailscale serve status --json`: an
identical entry is skipped, a different target for that port **refuses the whole run**, and Funnel is never used, the step
refuses when Funnel is on for the port and asserts that the resulting serve config has no `AllowFunnel` entry); Syncthing device and folder (staggered versioning, `.git` ignored); and finally **deploy
registration**: the instance is added to `instances` in `<data>/deploy/installed.json` with `service_cmds` that stop and
start it through the launcher, so every deploy restarts and health-checks it. All checks run before any change: a port
already served elsewhere, a non-restricted env file at the target or a Syncthing folder already using the path **refuses the
whole run** with no change.

### Security model

- The web app never runs as root and never starts a process for onboarding. The root part is a root-owned file; its
  request files are re-validated (name, email, folder, domain, memory, device ID) before anything runs, symlinks are not
  followed, and the sudoers rule names three exact commands.
- The only route reachable without a session is `POST /api/enroll/report`. It accepts one JSON object of four fields, only
  from tailnet addresses (never through the Cloudflare tunnel; it is `404` on restricted instances; see section 3.1), only with a valid
  token, and only once. The token is random, single-use, valid two hours, stored as a hash, and replaced when you generate a
  new command. Wrong, expired and reused tokens look identical and are rate limited per source.
- The pasted line contains the script, a single-use preauthorized Tailscale auth key, the manager's public SSH key, the
  folder, the manager's tailnet URL and Syncthing device ID, and the one-time token. It never contains the Cloudflare,
  Syncthing or Tailscale API keys. It is not downloaded from anywhere.
- Onboarding never touches the Cloudflare tunnel `linko-messenger-webhook` (production Messenger traffic) and never uses
  Tailscale Funnel.

### 3.1 The trusted-proxy setting

Tailscale Serve connects to the manager from loopback and sets `X-Forwarded-For` to the tailnet address it saw (plus
`Tailscale-User-*` identity headers). The manager (`ai_manager/proxy.py`, `Settings.trusted_proxies`) believes forwarded
headers **only** when the direct TCP peer is a configured trusted proxy, and `/api/enroll/report` additionally requires
that the connection arrived on the manager's own listener port. The client address is the **right-most untrusted**
`X-Forwarded-For` hop, so a client that sends its own header cannot choose it. `CF-Connecting-IP`/`CF-Ray` are never
believed on that route and `Tailscale-User-*` headers are never read: the enrollment token stays the only credential.

| Request | Result |
| --- | --- |
| peer loopback, `X-Forwarded-For` ends in a tailnet address (`100.64.0.0/10`) | accepted |
| peer loopback, forwarded address outside the tailnet, or no header | `403` |
| peer not trusted, any headers | headers ignored; `403` unless the peer itself is a tailnet address |

Setting: `AI_MANAGER_TRUSTED_PROXIES` in the manager's env file, a comma list of IPs/CIDRs. The default `127.0.0.1,::1` is
exactly Tailscale Serve and is enough; set it explicitly (`AI_MANAGER_TRUSTED_PROXIES=127.0.0.1`) only if it was changed.
Never add `100.64.0.0/10` or other tailnet ranges: whatever a trusted peer sends is believed. Onboarding makes no edit to
the live configuration.

### 3.2 Isolation

A user's PC must reach **only its own instance port**, plus the manager's `:<manager port>` for `/api/enroll/report`: not other users'
instances and not unrelated tailnet services. Two independent layers enforce it. The first works today with no Tailscale
credentials; the second needs the policy change below, applied once by the owner.

**Layer 1: app-level allow-list (automatic).** When the PC's enrollment report arrives, Apply writes
`AI_MANAGER_ALLOWED_CLIENTS=<the PC's Tailscale IP>` into the instance's env file (step **client allow-list**; idempotent;
an existing different value is refused, not overwritten; an existing running instance is restarted with `systemctl try-restart`).
An instance with the setting rejects every request whose client address is not in the list with `403`, before the host check,
login and every route. Loopback is always allowed so health checks and deploys work. The address comes from the same
trusted-proxy logic as 3.1, stricter: `X-Forwarded-For` is only believed from a trusted peer (loopback = Tailscale Serve), the
**right-most** hop is used, `CF-Connecting-IP` is never read, and an unparsable hop denies. A device that connects to the
instance directly is judged by its own address; headers it sends change nothing. The setting takes IPs and CIDRs, comma separated;
a bad entry stops the start (fail closed). Unset means no restriction, so the main instance is unchanged. If no PC address is
known (the device ID was typed by hand), the step does not exist and the screen warns that the instance is not restricted to one
client; Apply again after the PC has reported in.

**Layer 2: Tailscale policy (generated, applied by you).** Every record gets the tag `tag:linko-<name>`; the PC's auth key is
minted with it. The Add User receipt (and the screen while enrolling) shows the matching policy snippet for that user, e.g. for
`anna` on port 23417 (manager at `100.100.0.1`):

```json
{
  "hosts": {"linko-manager": "100.100.0.1"},
  "tagOwners": {"tag:linko-anna": ["autogroup:admin"]},
  "grants": [
    {"src": ["tag:linko-anna"], "dst": ["linko-manager"], "ip": ["tcp:23417", "tcp:31847"]},
    {"src": ["linko-manager"], "dst": ["tag:linko-anna"], "ip": ["tcp:22", "tcp:22000"]}
  ]
}
```

That is all it grants: the tag reaches the manager host on its own port and the manager port (31847 in this example); the manager reaches the tag on 22 (SSH)
and 22000 (Syncthing, which the manager initiates). Merge each user's `hosts`, `tagOwners` and `grants` entries into the tailnet
policy file (*Access controls*; the same `linko-manager` alias is defined once). `ai_manager/tailnet_policy.py` generates it; the
app never calls the Tailscale API to change the policy and never edits the tailnet's rules. The snippet appears once the plan has
allocated the user's port (it is the `tcp:` port of the user's address). The previewed port is saved on the record and sent with every later request, so Apply uses the same port (and the same snippet) unless it has meanwhile become busy; an existing instance always keeps the port in its env file. Apply the `tagOwners` entry **before** generating the
PC command, otherwise Tailscale refuses to create a key with that tag.

**The default allow-all rule must be removed for layer 2 to hold.** A tailnet that still has the default
`{"src": ["*"], "dst": ["*"], "ip": ["*"]}` grant (or the legacy `acls` `*:*` rule) lets every device reach every other, whatever
is granted here. Grants only add access. This is a change the owner makes in the admin console; the software does not touch it.
Before removing it, make sure your own devices keep access (for example a grant for `autogroup:member` or your own user to
what you use), or you lock yourself out. Until it is removed only layer 1 isolates.

**Credentials for automatic key minting (optional).** Without them the screen shows a field to paste one single-use auth key
(create it in the admin console with the tag `tag:linko-<name>`, *Reusable* off, *Ephemeral* off, *Pre-approved* on). For
automatic minting give the manager one of:

- An **API access token** (*Settings → Keys → Generate access token*; valid up to 90 days). It acts as its creator, so create it
  as an Owner/Admin/Network admin; it can create auth keys for any tag that user owns. In the onboarding config:
  `TAILSCALE_API_KEY=tskey-api-…` and `TAILSCALE_TAILNET=<tailnet name or ->`.
- or an **OAuth client** (*Settings → OAuth clients → Generate*) with exactly the scope **`auth_keys` → Write** and the
  **tags it may assign**: list every `tag:linko-<name>` you onboard (wildcards are not supported; the tags must exist in
  `tagOwners`, and a new user's tag is added to the client). This version sends `TAILSCALE_API_KEY` as the bearer token and does
  **not** perform the OAuth token exchange itself: use an API access token, or paste a single-use key per user, until that is added.

Either way the credential is only the mint of single-use keys; it is never in the PC command and never in git.

**Verify.** From the user's PC the instance opens (`curl -sI https://<manager tailnet name>:<port>/` returns `200` or a login
redirect). From **another tailnet device** (not that PC): `curl -m 10 -sI https://<manager tailnet name>:<port>/` returns
`403` (layer 1) or times out / is refused (layer 2 in force, usually the stronger result), and the same for another user's port and
for `:<manager port>/api/enroll/report` (`403` or timeout). On the manager, `curl -sI http://127.0.0.1:<port>/api/auth-status` stays `200`
(health checks). In the admin console, *Machines* shows the PC with only its `tag:linko-<name>` tag.

**Rollback.** Layer 1: remove the `AI_MANAGER_ALLOWED_CLIENTS=` line from the instance's env file and
`systemctl restart linko-bot-manager-<name>`; the instance is open to the tailnet again as before. Layer 2: delete that user's
`grants`, `tagOwners` and (when no user remains) the `linko-manager` host from the policy file, and restore your previous
rule (for example the default allow-all) if you removed it; Tailscale keeps the policy history in the admin console. Devices
already enrolled keep their tag until you change it (*Machines → Edit ACL tags*). Reverting the code (`git revert`, then
`sudo python3 scripts/install_onboarding.py install`) returns to the shared `tag:linko-pc`, which no longer matches your
policy: keep or restore the old `tag:linko-pc` entries before doing so.

## 4. Command-line fallback

Everything the screen does can be done by hand as root (a dry run unless `--apply`):

```bash
# on the PC, Administrator PowerShell (or paste the screen's line instead)
.\Linko-Setup.ps1 -TailscaleAuthKey <key> -ManagementPublicKey "<ssh-ed25519 AAAA... manager>"
# prints hostname, Tailscale IP and Syncthing device ID

# on the server
sudo -E .venv/bin/python -m ai_manager.provision add-user --name anna --email anna@example.com \
  --windows-folder 'C:\Users\Anna\Documents\Work' --syncthing-device-id <ID> \
  --service-user <manager user>          # adds the scoped ACL steps; review the plan, then repeat with --apply
```

Without `--domain` the run is tailnet-only (no Cloudflare step); `--skip-syncthing` omits the Syncthing part. The CLI leaves `<data>/initial-login.txt` (mode 0600) for you
to hand over and delete. Afterwards register deploys with `python3 scripts/install_deploy.py --instance anna` as the
manager user (after copying the env file as in INSTALL section 6) and set `service_cmds` as described in the README.

## 5. Troubleshooting

- **"The host helper is not installed"** — section 2.3. Check `python3 scripts/install_onboarding.py status`; the path unit
  must be `active` (`systemctl --user status linko-bot-manager-onboarding.path`).
- **Plan or apply never answers (record fails after 5 minutes / 3 hours)** — `journalctl --user -u linko-bot-manager-onboarding`;
  typical: the sudoers rule is missing (re-run `install`), `/etc/linko/onboarding.json` names another data directory, or
  `sudo: a password is required` (the unit must not have `NoNewPrivileges`).
- **Refused: tailnet serve ... already served to ...** — that port is published for something else (`tailscale serve status`);
  nothing was changed. Free the port by hand or let the next instance take another one. **Refused: Funnel is on** — turn it
  off (`tailscale funnel --https=<port> off`); onboarding never enables or uses Funnel.
- **"cannot determine the tailnet hostname" / manager URL errors** — set `LINKO_MANAGER_URL` (with its port) in the
  onboarding config, or log the machine in to Tailscale and make sure `tailscale serve status` lists the manager's port.
- **Missing SYNCTHING_API_KEY** — section 2.2; the plan shows it before anything changes. (Cloudflare errors: Appendix A.)
- **"Tailscale refused to create an auth key"** — the API key expired, the tailnet name is wrong, or the user's tag
  `tag:linko-<name>` is not in `tagOwners` yet (apply the snippet from the receipt screen first, see 3.2).
- **The user gets `403 Forbidden` on their own instance** — the allow-list does not contain the address the request arrives
  from. Tailscale Serve reports the PC's address in `X-Forwarded-For`; if the PC reaches the manager over IPv6 that is its
  `fd7a:…` address, not the IPv4 it reported. Append it: `AI_MANAGER_ALLOWED_CLIENTS=100.x.y.z,fd7a:115c:a1e0::abcd`
  in the instance's env file, then restart the unit. A step `refused: … already has AI_MANAGER_ALLOWED_CLIENTS=` means the value
  was set by hand; the app never overwrites it.
- **The PC cannot report in** — it must reach `LINKO_MANAGER_URL` over Tailscale (policy `grants`, Tailscale Serve running).
  A `403` means the request did not arrive from a tailnet address (Tailscale Serve running, loopback trusted: section 3.1;
  the manager's `AI_MANAGER_PORT` must be the port Serve forwards to), the token was used already or it expired. Generate a new command; the PC script is safe to re-run. Meanwhile you can type the PC's
  Syncthing device ID under *Enter a device ID by hand*.
- **Syncthing cannot write the folder** — `getfacl /home/<name>/Documents/<name>` must list the Syncthing user; Retry re-applies.
- **No Tailscale IP on Windows** — the auth key expired or was used; generate a new command.
- **SSH refused** — the connection must come from the Tailscale range; check the `Linko SSH (Tailscale only)` rule and `sshd`.
- **Service down** — `journalctl -u linko-bot-manager-<name>`; check the port is not taken.
- **The user's URL does not open** — they must be on the tailnet; `tailscale serve status` on the server must list
  `https://<manager tailnet name>:<port>` with `proxy http://127.0.0.1:<port>`, and the instance's `AI_MANAGER_ALLOWED_HOSTS`
  must contain the tailnet name.
- **The initial password was not copied** — stop the unit, set a new login as that user:
  `sudo -u <name> sqlite3 ~<name>/.local/share/linko-bot-manager-<name>/manager.sqlite3 'DELETE FROM owner'`, then in a shell
  with that instance's env file loaded (`set -a; . ~<name>/.config/linko-bot-manager-<name>.env; set +a`) run
  `python -m ai_manager.cli setup-owner` and start the unit.

## 6. Removal by hand

There is deliberately no automatic removal. In order: stop publishing the instance, `tailscale serve --https=<port> off`
(only that port; the manager's own port stays; if the record used the Cloudflare appendix also delete its Access
application, DNS CNAME and tunnel ingress rule); remove the Syncthing folder `linko-<name>` and the device (Syncthing GUI); remove the `<name>` entry from
`instances` in `<data>/deploy/installed.json`; then `systemctl disable --now linko-bot-manager-<name>`, delete
`/etc/systemd/system/linko-bot-manager-<name>.service`, `systemctl daemon-reload`, and finally `userdel -r <name>` after
backing up what you need. Delete the onboarding record's row if you want the name free again
(`sqlite3 <data>/manager.sqlite3 "DELETE FROM onboarding WHERE name='<name>'"` while the manager is idle). On Windows, remove the
management key from `%ProgramData%\ssh\administrators_authorized_keys`, and uninstall Tailscale/Syncthing if wanted; in the
Tailscale admin console remove the machine. To remove the helper itself:
`sudo python3 scripts/install_onboarding.py uninstall` and `systemctl --user disable --now linko-bot-manager-onboarding.path`.

### Rollback of this tailnet-only change

The change is one commit: `git revert <commit>` and re-run `sudo python3 scripts/install_onboarding.py install` (the root
launcher is a copy of `ai_manager/provision.py`, `status` shows `launcher_current`). Instances already created keep
working: their `tailscale serve` entry and env files are plain host state (remove an entry with
`tailscale serve --https=<port> off`). The reverted code needs a domain and Cloudflare again. Nothing in the live
Tailscale, Cloudflare or Syncthing configuration is edited by installing or reverting the code itself.

## Appendix A: optional Cloudflare access (dedicated tunnel only)

Not needed for the default flow. Use it only if a user must be reached through a public Cloudflare hostname behind
Cloudflare Access. It is switched on **per request** by typing a domain under *Advanced → Cloudflare domain* (or
`--domain` on the CLI); no domain is ever filled in from the configuration, and `LINKO_DOMAIN` is not read. The run then has
the steps *cloudflare tunnel route*, *cloudflare dns cname* and *cloudflare access application* instead of *tailnet serve*,
and the instance accepts `<name>.<domain>` in `AI_MANAGER_ALLOWED_HOSTS`.

**The tunnel must be dedicated to onboarding.** Every Cloudflare step checks this in `check()`, before any write, and
refuses the whole run (no `PUT`, no `POST`, nothing else changed) unless all of these hold:

1. the tunnel is **not** named `linko-messenger-webhook` (the production Messenger tunnel; always refused, whatever else is configured);
2. the tunnel's name starts with `linko-onboarding`;
3. every hostname already in its ingress was created by onboarding: it is `<name>.<domain>` of a record in the manager's
   `onboarding` table, the hostname of this very run, or listed in `LINKO_ONBOARDING_HOSTNAMES` (comma-separated allow-list
   in the onboarding config). Any other hostname refuses.

Create a new remotely managed tunnel named e.g. `linko-onboarding` for this; never point `CLOUDFLARE_TUNNEL_ID` at the production one.
Additional config keys for this appendix:

| Key | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | Edit tunnel configuration, DNS and Access applications |
| `CLOUDFLARE_ACCOUNT_ID`, `CLOUDFLARE_ZONE_ID`, `CLOUDFLARE_TUNNEL_ID` | The account, zone and the **dedicated** tunnel |
| `LINKO_ONBOARDING_HOSTNAMES` | Optional allow-list of ingress hostnames onboarding may leave in place |

**Cloudflare API token.** The dedicated tunnel must be *remotely managed* (its ingress rules live in the dashboard), because the helper
edits them through the API. In the Cloudflare dashboard: *My Profile → API Tokens → Create Token → Create Custom Token*.
Permissions: `Account · Cloudflare Tunnel · Edit`, `Account · Access: Apps and Policies · Edit`, `Zone · DNS · Edit`.
Resources: the one account and the one zone only. No expiry you cannot live with; no IP filter unless the server's IP is
fixed. Account ID and zone ID are on the zone's *Overview* page (right column); the tunnel ID is under *Zero Trust →
Networks → Tunnels* (or `cloudflared tunnel list`).

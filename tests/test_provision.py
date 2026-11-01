"""ai_manager.provision with mocked system, Cloudflare and Syncthing calls; Linko-Setup.ps1 static checks."""
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest

from ai_manager import provision as pv

ROOT = Path(__file__).resolve().parents[1]
TUNNEL = 't' * 8


class World:
    """Fake host: commands, Cloudflare and Syncthing keep state; every call is logged."""

    def __init__(self, tmp):
        self.tmp, self.calls, self.users, self.enabled = tmp, [], set(), set()
        self.tunnel_name = 'linko-onboarding'  # Cloudflare steps only run on a tunnel dedicated to onboarding
        self.ingress = [{'service': 'http_status:404'}]
        self.serve = {}  # `tailscale serve status --json`
        self.tailnet = 'manager.example.ts.net'
        self.dns, self.apps = [], []
        self.st_devices = [{'deviceID': 'EXISTING'}]
        self.st_folders = [{'id': 'shared', 'path': '/data/shared'}]
        self.owner = None
        self.acls = {}

    def run(self, argv, **kw):
        self.calls.append(('run', argv))
        if argv[0] == 'tailscale':
            return self.tailscale(argv)
        if argv[0] == 'id':
            return (0, '1001') if argv[2] in self.users else (1, 'no such user')
        if argv[0] == 'useradd':
            self.users.add(argv[-1])
        if argv[0] == 'systemctl' and argv[1] == 'is-enabled':
            return (0, '') if argv[2] in self.enabled else (1, '')
        if argv[0] == 'systemctl' and argv[1] == 'enable':
            self.enabled.add(argv[-1])
        if argv[0] == 'getfacl':
            if not Path(argv[-1]).exists():
                return 1, 'No such file or directory'
            lines = ['# file: ' + argv[-1], 'user::rwx', 'group::---', 'other::---']
            return 0, '\n'.join(lines + [f'{key}:{perm}   #effective:{perm}' for key, perm in self.acls.get(argv[-1], {}).items()])
        if argv[0] == 'setfacl':
            parts = argv[argv.index('-m') + 1].split(':')
            default = parts[0] == 'd'
            who, perm = parts[-2], parts[-1]
            self.acls.setdefault(argv[-1], {})[('default:' if default else '') + 'user:' + who] = perm.replace('X', 'x')
            return 0, ''
        if argv[0] == 'runuser' and 'git' in argv:
            (Path(argv[-1]) / '.git').mkdir(exist_ok=True)
        if argv[0] == 'runuser' and 'create-owner' in argv:
            data = Path(next(a for a in argv if a.startswith('AI_MANAGER_DATA=')).split('=', 1)[1])
            with sqlite3.connect(data / 'manager.sqlite3') as conn:
                conn.execute('CREATE TABLE owner(id INTEGER, username TEXT)')
                conn.execute('INSERT INTO owner VALUES(1,?)', (argv[-1],))
        return 0, ''

    def tailscale(self, argv):
        if argv[1:3] == ['status', '--json']:
            return 0, json.dumps({'Self': {'DNSName': self.tailnet + '.'}})
        if argv[1:4] == ['serve', 'status', '--json']:
            return 0, json.dumps(self.serve)
        assert argv[1:3] == ['serve', '--bg'] and argv[3].startswith('--https=') and len(argv) == 5, argv  # never funnel
        port = argv[3].split('=')[1]
        self.serve.setdefault('TCP', {})[port] = {'HTTPS': True}
        self.serve.setdefault('Web', {})[f'{self.tailnet}:{port}'] = {'Handlers': {'/': {'Proxy': argv[4]}}}
        return 0, ''

    def http(self, method, url, headers=None, body=None):
        self.calls.append((method, url, body))
        ok = lambda r: (200, {'success': True, 'result': r})
        if '/cfd_tunnel/' in url:
            if not url.endswith('/configurations'):
                return ok({'name': self.tunnel_name})
            if method == 'GET':
                return ok({'config': {'ingress': list(self.ingress)}})
            self.ingress = body['config']['ingress']
            return ok({})
        if '/dns_records' in url:
            if method == 'GET':
                return ok(list(self.dns))
            self.dns.append({'type': 'CNAME', 'content': body['content']})
            return ok({})
        if '/access/apps' in url:
            if method == 'GET':
                return ok(list(self.apps))
            self.apps.append(body)
            return ok({})
        if url.endswith('/rest/config/devices'):
            return 200, list(self.st_devices)
        if '/rest/config/devices/' in url:
            self.st_devices.append(body)
            return 200, None
        if url.endswith('/rest/config/folders'):
            return 200, list(self.st_folders)
        if '/rest/config/folders/' in url:
            self.st_folders.append(body)
            return 200, None
        if url.endswith('/rest/system/status'):
            return 200, {'myID': 'LOCALID'}
        if '/rest/db/ignores' in url:
            self.ignores = body
            return 200, None
        raise AssertionError(url)

    def plan(self, apply, **over):
        base = self.tmp / 'home'
        base.mkdir(exist_ok=True)
        units = self.tmp / 'units'
        units.mkdir(exist_ok=True)
        config = {'CLOUDFLARE_API_TOKEN': 'cf-token-xyz', 'CLOUDFLARE_ACCOUNT_ID': 'acc', 'CLOUDFLARE_ZONE_ID': 'zone',
                  'CLOUDFLARE_TUNNEL_ID': TUNNEL, 'SYNCTHING_API_KEY': 'st-key-xyz'}
        args = dict(name='anna', email='anna@example.com', windows_folder='C:\\Work', domain='example.com', repo=ROOT,
                    python='/venv/bin/python', home_base=base, config=config, apply=apply, syncthing_device='WINDEV',
                    systemd_dir=units, run=self.run, http=self.http, port_free=lambda p: True)
        args.update(over)
        return pv.Plan(**args)

    def mutating(self):
        return [c for c in self.calls if (c[0] == 'run' and c[1][0] in ('useradd', 'chown', 'runuser', 'setfacl')) or
                (c[0] == 'run' and c[1][0] == 'tailscale' and 'status' not in c[1]) or c[0] in ('PUT', 'POST')]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'chmod', lambda *a, **k: None)
    w = World(tmp_path)
    return w


def status(receipt):
    return {t: s for t, s, _ in receipt}


def test_dry_run_has_no_side_effects(world):
    receipt = pv.provision(world.plan(False))
    assert set(status(receipt).values()) == {'planned'}
    assert world.mutating() == []
    assert not list((world.tmp / 'home').iterdir())
    assert not list((world.tmp / 'units').iterdir())


def test_apply_then_second_run_skips_everything(world):
    first = pv.provision(world.plan(True))
    assert set(status(first).values()) == {'created', 'allocated', 'started'}
    plan = world.plan(True)
    assert (plan.project / '.git').exists() and plan.env_file.is_file()
    assert (world.tmp / 'units' / plan.unit_name).read_text().count('MemoryMax=1G') == 1
    before = len(world.mutating())
    second = pv.provision(world.plan(True))
    assert set(status(second).values()) == {'skipped'}
    assert len(world.mutating()) == before


def test_hostname_routed_elsewhere_is_refused_before_any_change(world):
    world.ingress.insert(0, {'hostname': 'anna.example.com', 'service': 'http://127.0.0.1:8001'})
    receipt = pv.provision(world.plan(True))
    assert status(receipt)['cloudflare tunnel route'] == 'refused'
    assert world.mutating() == [] and 'anna' not in world.users


def test_dns_conflict_is_refused(world):
    world.dns.append({'type': 'CNAME', 'content': 'someone-else.cfargotunnel.com'})
    assert status(pv.provision(world.plan(True)))['cloudflare dns cname'] == 'refused'
    assert world.mutating() == []


def test_existing_syncthing_shares_untouched(world):
    pv.provision(world.plan(True))
    puts = [c for c in world.calls if c[0] in ('PUT', 'POST') and 'rest' in c[1]]
    assert all('shared' not in c[1] and 'EXISTING' not in c[1] for c in puts)
    assert world.st_folders[0] == {'id': 'shared', 'path': '/data/shared'} and world.st_devices[0] == {'deviceID': 'EXISTING'}
    folder = next(f for f in world.st_folders if f['id'] == 'linko-anna')
    assert folder['versioning']['type'] == 'staggered' and {'deviceID': 'WINDEV'} in folder['devices']
    assert '.git' in world.ignores['ignore']


def test_syncthing_folder_path_in_use_refused(world):
    world.st_folders.append({'id': 'x', 'path': str(world.plan(True).project)})
    assert status(pv.provision(world.plan(True)))['syncthing folder'] == 'refused'
    assert world.mutating() == []


def test_existing_owner_not_replaced(world):
    plan = world.plan(True)
    plan.data.mkdir(parents=True)
    with sqlite3.connect(plan.data / 'manager.sqlite3') as conn:
        conn.execute('CREATE TABLE owner(id INTEGER, username TEXT)')
        conn.execute("INSERT INTO owner VALUES(1,'boss')")
    receipt = pv.provision(world.plan(True))
    assert status(receipt)['app login'] == 'skipped'
    assert not any(c[0] == 'run' and 'create-owner' in c[1] for c in world.calls)


def test_generated_files_hold_no_credentials(world):
    pv.provision(world.plan(True))
    plan = world.plan(True)
    plan.port = int(re.search(r'PORT=(\d+)', plan.env_file.read_text()).group(1))
    texts = [plan.env_file.read_text(), (world.tmp / 'units' / plan.unit_name).read_text()]
    assert 'AI_MANAGER_RESTRICTED=1' in texts[0]
    for text in texts:
        assert 'xyz' not in text and 'TOKEN' not in text and 'KEY' not in text
    assert f'AI_MANAGER_PORT={plan.port}' in texts[0] and 8788 <= plan.port <= 8899


def test_port_skips_ports_in_other_env_files(world):
    other = world.tmp / 'home' / 'bob/.config'
    other.mkdir(parents=True)
    (other / 'linko-bot-manager-bob.env').write_text('AI_MANAGER_PORT=8788\n')
    plan = world.plan(False)
    pv.provision(plan)
    assert plan.port == 8789


def test_missing_credentials_refuse_without_changes(world):
    receipt = pv.provision(world.plan(True, config={}))
    assert 'refused' in status(receipt).values() and world.mutating() == []


def test_config_file_and_env_precedence(tmp_path):
    file = tmp_path / 'c.env'
    file.write_text('CLOUDFLARE_API_TOKEN=from-file\nSYNCTHING_API_KEY=k\n')
    assert pv.load_config(file, {'CLOUDFLARE_API_TOKEN': 'from-env'}) == {'CLOUDFLARE_API_TOKEN': 'from-env', 'SYNCTHING_API_KEY': 'k'}


def test_cli_defaults_to_dry_run(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(pv.subprocess, 'run', lambda argv, **k: subprocess.CompletedProcess(argv, *((0, '{}', '') if argv[0] == 'tailscale' else (1, '', ''))))  # nothing real runs
    code = pv.main(['add-user', '--name', 'anna', '--email', 'a@b.co', '--windows-folder', 'C:\\W',
                    '--skip-syncthing', '--home-base', str(tmp_path), '--config', str(tmp_path / 'none')],
                   environ={'LINKO_MANAGER_URL': 'https://m.example.ts.net:8787'})
    assert code == 0 and 'DRY RUN' in capsys.readouterr().out and not list(tmp_path.iterdir())


def test_powershell_script_static():
    text = (ROOT / 'deploy/windows/Linko-Setup.ps1').read_text()
    assert re.search(r'\[string\]\$TailscaleAuthKey', text) and re.search(r'\[string\]\$ManagementPublicKey', text)
    assert '100.64.0.0/10' in text and 'sshd' in text and 'syncthing' in text.lower()
    assert not re.search(r'tskey-[A-Za-z0-9-]{8,}', text)
    assert 'PRIVATE KEY' not in text and not re.search(r'ssh-(ed25519|rsa) AAAA', text)


def test_onboarding_doc_linked():
    assert (ROOT / 'docs/ONBOARDING.md').is_file()
    assert 'ONBOARDING.md' in (ROOT / 'README.md').read_text() and 'ONBOARDING.md' in (ROOT / 'docs/INSTALL.md').read_text()


def test_acl_steps_are_scoped_to_the_new_folder_and_idempotent(world):
    over = dict(service_user='boss', syncthing_user='syncd')
    first = status(pv.provision(world.plan(True, **over)))
    assert first['syncthing user access to the folder'] == 'created' and first['manager access to the instance data'] == 'created'
    plan = world.plan(True, **over)
    calls = [c[1] for c in world.calls if c[0] == 'run' and c[1][0] == 'setfacl']
    allowed = {plan.home, plan.project.parent, plan.project, plan.data, plan.home / '.local', plan.home / '.local/share'}
    assert calls and {a[-1] for a in calls} <= {str(p) for p in allowed}
    assert {a[-1] for a in calls if '-R' in a} == {str(plan.project), str(plan.data)}  # recursion: only these two folders
    for argv in calls:  # named users only: never group, other, mask or a chmod
        assert re.fullmatch(r'(d:)?u:(syncd|boss|anna):[r-][w-][xX-]', argv[argv.index('-m') + 1]), argv
    assert not [c for c in world.calls if c[0] == 'run' and c[1][0] == 'chmod']
    # the Syncthing user sees the parents only as search (x), the folder itself fully
    assert world.acls[str(plan.home)]['user:syncd'] == '--x' and world.acls[str(plan.project)]['user:syncd'] == 'rwx'
    assert world.acls[str(plan.project)]['default:user:anna'] == 'rwx'
    before = len(calls)
    second = pv.provision(world.plan(True, **over))
    assert set(status(second).values()) == {'skipped'}
    assert len([c for c in world.calls if c[0] == 'run' and c[1][0] == 'setfacl']) == before


def test_acl_dry_run_changes_nothing_and_plans_only_missing_entries(world):
    receipt = status(pv.provision(world.plan(False, service_user='boss', syncthing_user='syncd')))
    assert receipt['syncthing user access to the folder'] == 'planned' and not world.acls
    assert not [c for c in world.calls if c[0] == 'run' and c[1][0] == 'setfacl']


def test_acl_steps_absent_without_users_or_with_skip_syncthing(world):
    titles = [t for t, _, _ in pv.provision(world.plan(False))]
    assert not [t for t in titles if 'user access' in t or 'manager access' in t]
    titles = [t for t, _, _ in pv.provision(world.plan(False, service_user='boss', syncthing_user='syncd', skip_syncthing=True))]
    assert 'manager access to the instance data' in titles and 'syncthing user access to the folder' not in titles


def test_preview_before_the_pc_reports_in_plans_the_device_but_apply_refuses(world):
    preview = status(pv.provision(world.plan(False, syncthing_device='')))
    assert preview['syncthing device'] == 'planned'
    assert status(pv.provision(world.plan(True, syncthing_device='')))['syncthing device'] == 'refused'
    assert world.mutating() == []


def test_request_validation_is_strict():
    ok = dict(name='anna', email='a@b.co', windows_folder='C:\\Work', domain='example.com', memory_max='2G',
              device_id='-'.join(['ABCDEFG'] * 8))
    assert pv.validate_request(ok)['memory_max'] == '2G'
    for field, bad in (('name', 'Anna'), ('name', 'a;b'), ('name', '../x'), ('email', 'x y@z.co'), ('windows_folder', "C:\\a'b"),
                       ('windows_folder', 'C:\\a$b'), ('windows_folder', 'C:\\a\nb'), ('domain', 'a b.com'),
                       ('memory_max', '1G; rm'), ('device_id', 'nope')):
        with pytest.raises(pv.ProvisionError):
            pv.validate_request({**ok, field: bad})


def test_powershell_script_enrollment_static():
    text = (ROOT / 'deploy/windows/Linko-Setup.ps1').read_text()
    for name in ('WindowsFolder', 'ManagerUrl', 'ManagerDeviceId', 'FolderId', 'EnrollmentToken'):
        assert re.search(rf'\[string\]\${name}\b', text)
    assert 'api/enroll/report' in text and 'autoAcceptFolders' in text and '/rest/config/folders/' in text
    assert not re.search(r'(CLOUDFLARE|SYNCTHING_API|TAILSCALE_API)', text)

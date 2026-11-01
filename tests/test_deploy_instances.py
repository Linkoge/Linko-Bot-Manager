"""One shared code copy, several instances: a deploy backs up, restarts and health-checks every configured instance."""
import importlib.util
import json
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from test_deploy_script import CTL, ROOT, commit, deploy_script, git
from test_deploy_script import runtime as runtime_fixture

runtime = runtime_fixture


class Work:
    """A restricted second instance of the runtime's code, run by its own service-control script."""

    def __init__(self, runtime):
        self.runtime, root = runtime, runtime.root
        self.data, self.state, self.project, self.broken = root / 'work-data', root / 'work-service', root / 'work', root / 'broken'
        for folder in (self.state, self.project):
            folder.mkdir()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            self.port = probe.getsockname()[1]
        env = {'AI_MANAGER_DATA': str(self.data), 'AI_MANAGER_PORT': str(self.port), 'AI_MANAGER_RESTRICTED': '1',
               'AI_MANAGER_PROJECT': str(self.project)}
        # The instance refuses to come up on any release other than the base while the `broken` marker exists.
        (root / 'ctl-work.py').write_text(
            f'import os, subprocess, sys\nos.environ.update({env!r})\n'
            f"head = subprocess.run(['git', '-C', sys.argv[3], 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()\n"
            f"if sys.argv[1] == 'start' and os.path.exists({str(self.broken)!r}) and head != {runtime.base!r}:\n"
            "    sys.exit(0)\n" + CTL)
        ctl = [sys.executable, str(root / 'ctl-work.py')]
        self.entry = {'name': 'work', 'data': str(self.data), 'url': f'http://127.0.0.1:{self.port}',
                      'service_cmds': {'stop': [*ctl, 'stop', str(self.state), str(runtime.repo)],
                                       'start': [*ctl, 'start', str(self.state), str(runtime.repo)]}}
        runtime.config['instances'] = [self.entry]
        runtime.deploy = deploy_script.Deploy(runtime.config)

    def start(self):
        """Both services, as installed: the main one and this instance."""
        subprocess.run(self.runtime.config['service_cmds']['start'], check=True)
        subprocess.run(self.entry['service_cmds']['start'], check=True)
        self.runtime.deploy.wait_health(self.runtime.base)

    def stop(self):
        subprocess.run(self.entry['service_cmds']['stop'], check=False)

    def sql(self, query, args=()):
        with sqlite3.connect(self.data / 'manager.sqlite3', timeout=10) as conn:
            return conn.execute(query, args).fetchall()

    def running(self):
        status = deploy_script.Deploy(self.runtime.config).http('/api/auth-status', url=self.entry['url'])
        return json.loads(status[1])['release_commit']


@pytest.fixture
def work(runtime):
    value = Work(runtime)
    yield value
    value.stop()


def test_deploy_restarts_backs_up_and_health_checks_every_instance(runtime, work):
    work.start()
    runtime.sql("INSERT INTO settings VALUES('main-survivor','1')")
    work.sql("INSERT INTO settings VALUES('work-survivor','1')")
    new = commit(runtime.repo, 'docs change', **{'README.md': ('# Linko Bot Boss', '# Linko Bot Boss (deployed)')})
    runtime.request()
    result = runtime.run()
    assert result['status'] == 'success' and result['commit'] == new
    assert runtime.running() == new and work.running() == new
    backup = Path(result['backup'])
    assert sqlite3.connect(backup / 'before.sqlite3').execute("SELECT 1 FROM settings WHERE key='main-survivor'").fetchone()
    assert sqlite3.connect(backup / 'work' / 'before.sqlite3').execute(
        "SELECT 1 FROM settings WHERE key='work-survivor'").fetchone()
    assert work.sql("SELECT value FROM settings WHERE key='work-survivor'") == [('1',)]
    assert work.sql("SELECT count(*) FROM sessions") == [(0,)]  # the instance's health check cleans up after itself
    # Restart order: the instance is stopped and started like the main service, under its own fence.
    assert {'stop:fenced', 'start:fenced'} <= set((work.state / 'probe').read_text().split())


def test_unhealthy_instance_rolls_back_the_shared_code_and_every_database(runtime, work):
    work.start()
    runtime.sql("INSERT INTO settings VALUES('main-before','kept')")
    work.sql("INSERT INTO settings VALUES('work-before','kept')")
    bad = commit(runtime.repo, 'release the work instance cannot run', **{
        'README.md': ('# Linko Bot Boss', '# Linko Bot Boss (bad for work)')})
    work.broken.write_text('x')
    runtime.config['health_seconds'] = 5
    runtime.request()
    result = runtime.run()
    assert result['status'] == 'rolled_back' and result['commit'] == bad and 'instance work' in result['reason']
    assert git(runtime.repo, 'rev-parse', 'HEAD') == runtime.base
    work.broken.unlink()
    deploy_script.Deploy(runtime.config).wait_health(runtime.base)  # main and work both serve the old release again
    assert runtime.running() == runtime.base and work.running() == runtime.base
    assert runtime.sql("SELECT value FROM settings WHERE key='main-before'") == [('kept',)]
    assert work.sql("SELECT value FROM settings WHERE key='work-before'") == [('kept',)]


def test_deploy_waits_for_work_running_in_any_instance(runtime, work):
    work.start()
    deploy = deploy_script.Deploy(runtime.config)
    assert deploy.busy() == {}
    work.sql("INSERT INTO executions(id,agent_id,status,started_at,delivery) VALUES('e1','manager','running',1,NULL)")
    assert deploy.busy() == {'work:unfinished_executions': 1}
    assert list(deploy.blockers()) == ['executions', 'tasks', 'messages', 'work:executions', 'work:tasks', 'work:messages']
    assert deploy.blockers()['work:executions'][0]['id'] == 'e1'
    assert [label for label, _ in deploy.databases()] == ['', 'work:']
    # An instance that was never started has no database yet and cannot block.
    deploy.instances[0]['data'] = Path(runtime.root) / 'never-started'
    assert deploy.busy() == {}


def test_instance_fences_are_held_together_or_not_at_all(runtime, work):
    from ai_manager import fence
    work.start()
    deploy = deploy_script.Deploy(runtime.config)
    with fence.hold(work.data, True):  # a merge or deploy already holds the instance's fence
        with pytest.raises(fence.Busy):
            deploy.fences(0)
        with fence.hold(runtime.data, True):  # the main fence was released again by the failed attempt
            pass
    with deploy.fences(0):
        with pytest.raises(fence.Busy):
            fence.hold(work.data, False).__enter__()


def test_external_project_deploys_never_touch_instances(runtime, work):
    deploy = deploy_script.Deploy(runtime.config)
    assert deploy.instances
    request = {'id': 'x', 'repo': str(runtime.repo), 'project_id': 'p', 'setup': {
        'service': 'other.service', 'backup_command': ['true'], 'health_command': ['true'], 'rollback_command': ['true']}}
    deploy.select_setup(request)
    assert deploy.instances == []


def test_installer_records_instances_from_their_env_files_and_keeps_hand_edits(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('install_deploy', ROOT / 'scripts/install_deploy.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    spec.loader.exec_module(module)
    repo, data, home, bin_dir = tmp_path / 'repo', tmp_path / 'data', tmp_path / 'home', tmp_path / 'bin'
    (repo / '.venv/bin').mkdir(parents=True)
    (repo / '.venv/bin/python').symlink_to(sys.executable)
    (repo / 'deploy').mkdir()
    for name in module.UNITS:
        (repo / 'deploy' / (name + '.in')).write_text((ROOT / 'deploy' / (name + '.in')).read_text())
    bin_dir.mkdir()
    (bin_dir / 'systemctl').write_text('#!/bin/sh\n')
    (bin_dir / 'systemctl').chmod(0o755)
    monkeypatch.setenv('PATH', f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (home / '.config').mkdir(parents=True)
    (home / '.config/linko-bot-manager-work.env').write_text(
        '# comment\nAI_MANAGER_RESTRICTED=1\nAI_MANAGER_DATA="/srv/work-data"\nAI_MANAGER_PORT=20788\n')
    module.install(repo, data, home, ['work'])
    config = json.loads((data / 'deploy/installed.json').read_text())
    assert config['instances'] == [{'name': 'work', 'service': 'linko-bot-manager@work.service',
                                    'data': '/srv/work-data', 'url': 'http://127.0.0.1:20788'}]
    # Re-running without --instance keeps the list, including edits such as service_cmds for another Linux user.
    config['instances'][0]['service_cmds'] = {'stop': ['sudo', '-n', 'stop-work'], 'start': ['sudo', '-n', 'start-work']}
    deploy_script.atomic(data / 'deploy/installed.json', config)
    module.install(repo, data, home)
    assert json.loads((data / 'deploy/installed.json').read_text())['instances'] == config['instances']
    with pytest.raises(SystemExit, match='Create'):
        module.install(repo, data, home, ['missing'])
    (home / '.config/linko-bot-manager-bad.env').write_text('AI_MANAGER_DATA=/x\n')
    with pytest.raises(SystemExit, match='AI_MANAGER_PORT'):
        module.install(repo, data, home, ['bad'])


def test_instance_unit_template_and_env_example_describe_a_restricted_instance():
    unit = (ROOT / 'deploy/linko-bot-manager@.service.in').read_text()
    assert 'EnvironmentFile=%h/.config/linko-bot-manager-%i.env' in unit and 'ai_manager.cli serve' in unit
    assert set(line.split('=')[0] for line in unit.splitlines() if '@' in line and line.startswith('Exec')) == {'ExecStart'}
    env = dict(line.split('=', 1) for line in (ROOT / 'deploy/instance.env.example').read_text().splitlines()
               if line and not line.startswith('#'))
    assert env['AI_MANAGER_RESTRICTED'] == '1' and env['AI_MANAGER_SECURE_COOKIE'] == '1'
    assert {'AI_MANAGER_PROJECT', 'AI_MANAGER_DATA', 'AI_MANAGER_PORT', 'AI_MANAGER_ALLOWED_HOSTS'} <= set(env)
    assert env['AI_MANAGER_PORT'] != '8787'


def test_service_installer_installs_the_template_and_enables_one_instance(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('install_service', ROOT / 'scripts/install_service.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    repo, home = tmp_path / 'repo', tmp_path / 'home'
    (repo / 'deploy').mkdir(parents=True)
    (repo / 'deploy/linko-bot-manager@.service.in').write_text((ROOT / 'deploy/linko-bot-manager@.service.in').read_text())
    units = home / '.config/systemd/user'
    units.mkdir(parents=True)
    calls = []
    monkeypatch.setattr(module.subprocess, 'run', lambda argv, **kwargs: calls.append(argv))
    with pytest.raises(SystemExit, match='Create'):
        module.install_instance(repo, home, units, '/usr/bin/codex', 'work', True)
    (home / '.config/linko-bot-manager-work.env').write_text('AI_MANAGER_RESTRICTED=1\n')
    module.install_instance(repo, home, units, '/usr/bin/codex', 'work', True)
    text = (units / 'linko-bot-manager@.service').read_text()
    assert f'WorkingDirectory={repo}' in text and 'AI_MANAGER_CODEX=/usr/bin/codex' in text and '@REPO@' not in text
    assert ['systemctl', '--user', 'enable', '--now', 'linko-bot-manager@work.service'] in calls
    with pytest.raises(SystemExit, match='lowercase'):
        module.install_instance(repo, home, units, '/usr/bin/codex', '../x', True)


def test_stale_instance_restarts_even_when_main_already_runs_target(runtime, work):
    work.start()
    target = commit(runtime.repo, 'main already updated', **{
        'README.md': ('# Linko Bot Boss', '# Linko Bot Boss (current main)')})
    # Simulate a prior restart that updated only the main service.
    subprocess.run(runtime.config['service_cmds']['stop'], check=True, timeout=60)
    subprocess.run(runtime.config['service_cmds']['start'], check=True, timeout=60)
    main_only = deploy_script.Deploy({**runtime.config, 'instances': []})
    main_only.wait_health(target)
    assert runtime.running() == target and work.running() == runtime.base
    runtime.request()
    result = runtime.run()
    assert result['status'] == 'success'
    assert result['previous'] == target
    assert runtime.running() == work.running() == target
    assert (Path(result['backup']) / 'work' / 'before.sqlite3').exists()

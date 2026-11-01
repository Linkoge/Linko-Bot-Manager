"""Real Chromium: the Add User dialog (form, plan preview, enrollment command, Apply gate) on the main instance only."""
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
PROGRAM = '''
import sys
sys.path.insert(0, 'tests')
import uvicorn
from fake_codex import FakeServer
from ai_manager.config import Settings
from ai_manager.web import create_app
s = Settings()
uvicorn.run(create_app(s, FakeServer(s)), host='127.0.0.1', port=s.port, log_level='error')
'''


def serve(tmp_path, restricted):
    (tmp_path / 'work').mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = {**os.environ, 'AI_MANAGER_DATA': str(tmp_path / 'data'), 'AI_MANAGER_PORT': str(port), 'AI_MANAGER_SECURE_COOKIE': '0',
           'AI_MANAGER_PROJECT_ROOTS': str(tmp_path / 'work'), 'AI_MANAGER_ONBOARDING_CONFIG': str(tmp_path / 'ob.env'),
           'PYTHONPATH': str(ROOT)}
    if restricted:
        env.update(AI_MANAGER_RESTRICTED='1', AI_MANAGER_PROJECT=str(tmp_path / 'work'))
    (tmp_path / 'ob.env').write_text('')
    (tmp_path / 'data/onboarding').mkdir(parents=True)
    (tmp_path / 'data/onboarding/installed.json').write_text('{}')
    process = subprocess.Popen([sys.executable, '-c', PROGRAM], env=env, cwd=ROOT)
    url = f'http://127.0.0.1:{port}'
    for _ in range(200):
        try:
            if httpx.get(url + '/api/auth-status', timeout=1).status_code == 200:
                return process, url
        except httpx.HTTPError:
            time.sleep(0.1)
    process.kill()
    raise AssertionError('server did not start')


def sign_in(page, url, tmp_path):
    page.goto(url)
    page.locator('#username').fill('owner')
    page.locator('#password').fill(secrets.token_urlsafe(16))
    page.locator('#setup-token').fill((tmp_path / 'data/setup-token').read_text())
    page.locator('#login-button').click()
    page.wait_for_function('()=>state.ready')


def test_add_user_dialog_walks_form_plan_and_enrollment(tmp_path):
    process, url = serve(tmp_path, restricted=False)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(viewport={'width': 420, 'height': 900})
            page.set_default_timeout(10000)
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            sign_in(page, url, tmp_path)
            page.locator('#project-menu-button').click()
            page.get_by_role('button', name='Add user').click()
            assert page.locator('#onboarding-dialog').is_visible()
            page.locator('#onboarding-body input[name=name]').fill('anna')
            page.locator('#onboarding-body input[name=email]').fill('anna@example.com')
            page.locator('#onboarding-body input[name=windows_folder]').fill('C:\\Users\\Anna\\Work')
            page.get_by_role('button', name='Preview plan').click()
            page.get_by_text('Checking the host…').wait_for()
            entries = page.evaluate("()=>api('/api/onboarding').then(r=>r.entries)")
            assert [e['name'] for e in entries] == ['anna'] and entries[0]['state'] == 'pending'
            # the host helper answers: write what the root launcher would
            result = {'id': entries[0]['id'], 'action': 'plan', 'finished': True,
                      'receipt': [['linux user', 'planned', 'useradd anna'], ['syncthing device', 'planned', 'add the Windows device']]}
            (tmp_path / 'data/onboarding/results' / (entries[0]['id'] + '.json')).write_text(json.dumps(result))
            page.get_by_text('[planned] linux user').wait_for()
            assert page.get_by_role('button', name='Apply').is_disabled()
            assert page.locator('#onboarding-body').inner_text().count('nothing has been changed') == 1
            page.get_by_role('button', name='Generate PC command').click()
            page.wait_for_function("()=>document.getElementById('onboarding-error').textContent.includes('auth key')")
            assert page.locator('#onboarding-auth-key').count() == 1  # no Tailscale API key configured: the owner pastes one
            assert not errors
            browser.close()
    finally:
        process.kill()


def test_restricted_instance_has_no_add_user_entry(tmp_path):
    process, url = serve(tmp_path, restricted=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page()
            page.set_default_timeout(10000)
            sign_in(page, url, tmp_path)
            assert page.locator('#onboarding-dialog').is_hidden()
            assert page.get_by_role('button', name='Add user').count() == 0
            assert page.evaluate("()=>fetch('/api/onboarding').then(r=>r.status)") == 403
            browser.close()
    finally:
        process.kill()

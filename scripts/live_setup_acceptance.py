"""Explicit live acceptance. Creates real remote identities only on 'start'."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
from amca.setup_node import write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['prepare', 'start', 'status', 'prove', 'stop', 'invite', 'rebind'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--exe')
    parser.add_argument('--invite-file')
    args = parser.parse_args()
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    command = [str(Path(args.exe).resolve())] if args.exe else [sys.executable, '-m', 'amca.setup_cli']
    env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'PYTHONUTF8': '1'}

    def cli(*parts):
        p = subprocess.run([*command, *map(str, parts)], env=env, capture_output=True, text=True,
                           encoding='utf-8', timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
        value = json.loads(p.stdout)
        if p.returncode:
            raise RuntimeError(json.dumps(value, ensure_ascii=False))
        return value

    def ui_action(payload):
        p = subprocess.Popen([*command, 'ui', '--registry-dir', str(root / 'ui-data'), '--no-open'],
                             env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             encoding='utf-8', creationflags=subprocess.CREATE_NO_WINDOW)
        line = p.stdout.readline().strip()
        if not line.startswith('UI_URL='):
            raise RuntimeError('UI did not start')
        url = urlparse(line[7:])
        token = parse_qs(url.fragment)['token'][0]
        def action(body):
            request = Request('http://' + url.netloc + '/api/hub/action', data=json.dumps(body).encode(),
                              headers={'Content-Type': 'application/json', 'X-Hub-Token': token})
            with urlopen(request, timeout=30) as response:
                return json.load(response)
        try:
            return action(payload)
        finally:
            action({'action': 'close'})
            p.wait(timeout=15)

    if args.action == 'prepare':
        if (root / 'claimed.json').exists():
            raise RuntimeError('Existing acceptance run: reuse it rather than create another identity')
        code = Path(args.invite_file).read_text(encoding='utf-8') if args.invite_file else ui_action({'action': 'create', 'space_name': '真实接入验收空间'})['code']
        code_file = root / 'entry.txt'
        code_file.write_text(code, encoding='utf-8')
        ready = cli('preflight', '--file', code_file, '--registry-dir', root / 'ui-data')
        assert ready['status'] == 'ready'
        claim = cli('accept', '--file', code_file, '--label', '开发验收当前工具会话', '--registry-dir', root / 'ui-data')
        write_json(root / 'claimed.json', claim)
        config = claim['template']
        config['card']['role_name'] = '接入验收助手'
        # Only synthetic acceptance traffic; no user private material or permissions.
        config['policy']['disclosure'] = {'allow': ['本次验收的合成测试文本'], 'deny': ['所有私人资料']}
        draft = root / 'configuration.json'
        write_json(draft, config)
        valid = cli('--data-dir', claim['data_dir'], 'validate', '--file', draft)
        result = cli('--data-dir', claim['data_dir'], 'apply', '--file', draft, '--confirmed-digest', valid['config_digest'])
        print(json.dumps({'status': result['status'], 'root': str(root)}, ensure_ascii=False))
        return
    claim = json.loads((root / 'claimed.json').read_text(encoding='utf-8'))
    if args.action == 'rebind':
        code = ui_action({'action': 'resume', 'entry_id': claim['entry_id']})['code']
        (root / 'entry.txt').write_text(code, encoding='utf-8')
        claim = cli('accept', '--file', root / 'entry.txt', '--label', '开发验收当前工具会话', '--registry-dir', root / 'ui-data')
        write_json(root / 'claimed.json', claim)
        print(json.dumps({'status': claim['status'], 'configuration_required': claim['configuration_required']}))
        return
    if args.action == 'prove':
        state = cli('--data-dir', claim['data_dir'], 'status')
        assert all(state.get(k) is True for k in ['connected', 'endpoint_reachable', 'a2a_handshake_ok', 'membership_verified']), state
        polled = cli('agent', 'poll', '--session-file', claim['session_file'])
        if polled.get('challenge'):
            challenge = polled['challenge']
            assert all(challenge[k] == state[k] for k in ['agent_id', 'space_id'])
            ack_file = root / 'challenge-reply.json'
            write_json(ack_file, {k: challenge[k] for k in ['challenge_id', 'nonce']})
            receipt = cli('agent', 'ack', '--session-file', claim['session_file'], '--file', ack_file)
            assert receipt['channel_verified'] is True
        result = cli('--data-dir', claim['data_dir'], 'status')
        assert result['binding']['channel_verified'] is True
        write_json(root / 'verified-receipt.json', result)
    elif args.action == 'invite':
        result = ui_action({'action': 'invite', 'entry_id': claim['entry_id']})
        (root / 'invitation.txt').write_text(result['code'], encoding='utf-8')
        result = {'space_id': result['space_id'], 'invitation_file': str(root / 'invitation.txt')}
    else:
        result = cli('--data-dir', claim['data_dir'], args.action)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()

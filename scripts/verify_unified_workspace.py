"""Browser acceptance against synthetic, space-isolated local chat data."""
import json
import sys
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch
from wsgiref.simple_server import make_server

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from amca.hub_ui import HubApp
from amca.setup_node import QuietHandler

with tempfile.TemporaryDirectory() as root:
    app = HubApp(root, 'browser-fixture-only')
    entries = [app.hub.create_code(name)['entry_id'] for name in ['合成甲空间', '合成乙空间']]
    histories = {entry: [{'sender_agent_id': 'peer-' + entry, 'kind': 'answer', 'status': 'answered',
                         'content': '独立历史 ' + entry, 'created_at': '2026-09-16T00:00:00Z'}] for entry in entries}
    real_overview = app.hub.overview
    def overview():
        value = real_overview()
        for s in value['spaces']:
            if s['entry_id'] in entries:
                s['state'] = {'connected': True, 'status': 'connected', 'binding': {'channel_verified': True},
                              'configuration': {'card': {'role_name': '合成身份'}}}
        return value
    calls = []
    def local(data, method, path, payload=None, **kwargs):
        entry = Path(data).name
        assert entry in entries
        calls.append((entry, method, path))
        if path == '/api/me': return {'agent_id': entry, 'display_name': '合成身份'}
        if path == '/api/members': return [{'agent_id': 'peer-' + entry, 'display_name': '合成成员 ' + entry[-4:]}]
        if path == '/api/conversations': return [{'conversation_id': 'bilateral-' + entry, 'counterpart_agent_id': 'peer-' + entry}]
        if path.endswith('/messages'): return histories[entry]
        if path == '/api/questions':
            assert payload['target_agent'] == 'peer-' + entry
            histories[entry].append({'sender_agent_id': entry, 'kind': 'question', 'status': 'delivered', 'content': payload['question'], 'created_at': '2026-09-16T01:00:00Z'})
            return {'conversation_id': 'bilateral-' + entry, 'request_id': 'synthetic', 'delivery_status': 'delivered'}
        if path == '/api/setup/approvals': return [{'request_id': 'synthetic', 'from_agent': 'fixture', 'question': '合成待确认问题'}]
        if path == '/api/setup/approve': return {'status': 'queued'}
        raise AssertionError(path)
    with patch.object(app.hub, 'overview', side_effect=overview), patch.object(app.hub, 'invitation', return_value={'code': '合成邀请，非真实凭证'}), patch('amca.hub_ui.local_call', side_effect=local):
        server = make_server('127.0.0.1', 0, app, handler_class=QuietHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            subprocess.run([sys.executable, 'scripts/verify_hub_browser.py', f'http://127.0.0.1:{server.server_port}/#token=browser-fixture-only', 'build/unified-workspace-browser', 'fixture'], check=True)
            assert len([x for x in calls if x[2] == '/api/questions']) == 1
            assert len([x for x in calls if x[2] == '/api/setup/approve']) == 1
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

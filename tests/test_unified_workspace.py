import tempfile
import unittest
from unittest.mock import patch
from tests.test_entry_hub import call
from amca.hub_ui import HubApp


class WorkspaceTests(unittest.TestCase):
    def test_chat_requires_hub_auth_and_scopes_to_selected_entry(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, 'local-test')
            first = app.hub.create_code('一')['entry_id']
            second = app.hub.create_code('二')['entry_id']
            request = {'entry_id': second, 'method': 'GET', 'path': '/api/conversations', 'payload': None}
            with patch('amca.hub_ui.local_call', return_value=[]) as node:
                self.assertEqual('403 Forbidden', call(app, request, token='wrong', path='/api/hub/chat')[0][0])
                node.assert_not_called()
                self.assertEqual('200 OK', call(app, request, path='/api/hub/chat')[0][0])
                self.assertEqual(app.hub.data(second), node.call_args.args[0])
                self.assertNotEqual(app.hub.data(first), node.call_args.args[0])

    def test_proxy_rejects_admin_paths_cross_origin_and_unknown_entries(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, 'local-test')
            entry = app.hub.create_code('测试')['entry_id']
            with patch('amca.hub_ui.local_call') as node:
                for target in ['/api/setup/approve', '/api/agent/ack', '/api/conversations/../messages', '/api/conversations/x/messages?token=x']:
                    result = call(app, {'entry_id': entry, 'method': 'POST', 'path': target}, path='/api/hub/chat')
                    self.assertEqual('403 Forbidden', result[0][0])
                self.assertEqual('400 Bad Request', call(app, {'entry_id': 'missing', 'method': 'GET', 'path': '/api/me'}, path='/api/hub/chat')[0][0])
                self.assertEqual('403 Forbidden', call(app, {'entry_id': entry, 'method': 'GET', 'path': '/api/me'}, path='/api/hub/chat', origin='https://elsewhere.test')[0][0])
                node.assert_not_called()

    def test_explicit_question_payload_reaches_only_selected_node(self):
        with tempfile.TemporaryDirectory() as root:
            app = HubApp(root, 'local-test')
            entry = app.hub.create_code('测试')['entry_id']
            question = {'target_agent': 'peer', 'question': '中文问题', 'topic': 'general', 'project_scope': 'general'}
            with patch('amca.hub_ui.local_call', return_value={'request_id': 'test'}) as node:
                self.assertEqual('200 OK', call(app, {'entry_id': entry, 'method': 'POST', 'path': '/api/questions', 'payload': question}, path='/api/hub/chat')[0][0])
                node.assert_called_once_with(app.hub.data(entry), 'POST', '/api/questions', question, timeout=30)

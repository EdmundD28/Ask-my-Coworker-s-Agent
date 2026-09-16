import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from tests import _bootstrap
from amca.transport import AcnNetworkAdapter, AgentRegistration, UrllibJsonHttpClient, JsonHttpError
from amca.setup_node import diagnose, write_json
from amca.setup_config import contract, validate_config


class RegistrationContractTests(unittest.TestCase):
    def test_preflight_does_not_claim_or_register(self):
        from amca.entry_hub import EntryHub, parse_entry
        from amca.setup_preflight import preflight
        with tempfile.TemporaryDirectory() as root:
            hub = EntryHub(Path(root) / 'ui-data')
            code = hub.create_code('检查环境')
            path = Path(root) / 'code.txt'
            path.write_text(code['code'], encoding='utf-8')
            with patch('amca.setup_preflight.UrllibJsonHttpClient') as client:
                self.assertEqual('ready', preflight(path, hub.root)['status'])
                client.return_value.request_json.assert_called_once()
                self.assertEqual('GET', client.return_value.request_json.call_args.args[0])
            self.assertFalse(hub.get(code['entry_id'])['claimed'])
            self.assertFalse(hub.data(code['entry_id']).exists())

    def test_wrong_security_context_is_blocked_before_starting_process(self):
        from amca.setup_cli import start
        from amca.setup_node import apply_config
        from amca.setup_config import config_digest
        from tests.test_conversational_setup import configuration
        with tempfile.TemporaryDirectory() as root:
            value = configuration()
            apply_config(root, value, config_digest(value))
            with patch('amca.setup_cli.check_execution_context', side_effect=PermissionError('wrong context')), patch('amca.setup_cli.subprocess.Popen') as launch:
                with self.assertRaises(PermissionError):
                    start(root)
                launch.assert_not_called()

    def test_optional_short_card_matches_remote_contract_without_inventing_identity(self):
        http = Mock()
        http.request_json.return_value = {'agent_id': 'fixture', 'api_key': 'test-only'}
        adapter = AcnNetworkAdapter('https://example.test', http=http)
        adapter.register_agent(AgentRegistration('乙', '未填写', (), 'https://example.test/a2a'))
        body = http.request_json.call_args.kwargs['payload']
        self.assertGreaterEqual(len(body['name']), 2)
        self.assertGreaterEqual(len(body['description']), 10)
        self.assertTrue(body['description'].endswith('未填写'))
        self.assertNotIn('owner_claim', body)

    def test_oversized_fields_fail_locally_before_registration(self):
        http = Mock()
        with self.assertRaises(ValueError):
            AcnNetworkAdapter('https://example.test', http=http).register_agent(
                AgentRegistration('x' * 101, 'valid description', (), 'https://example.test/a2a'))
        http.request_json.assert_not_called()
        config = contract()['template']
        config['space_name'] = '测试'
        config['card']['role_name'] = 'x' * 101
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_real_service_error_envelope_exposes_only_safe_constraints(self):
        body = {'details': {'pydantic_errors': [{'loc': ['body', 'description'], 'type': 'string_too_short',
                'msg': 'do not echo', 'input': 'private-value', 'ctx': {'min_length': 10}}]}}
        error = HTTPError('https://example.test', 422, 'bad request', {}, io.BytesIO(json.dumps(body).encode()))
        with patch('amca.transport.urlopen', side_effect=error), self.assertRaises(JsonHttpError) as caught:
            UrllibJsonHttpClient().request_json('POST', 'https://example.test', payload={})
        self.assertEqual([{'field': 'description', 'constraint': {'min_length': 10}}], caught.exception.validation_fields)
        with tempfile.TemporaryDirectory() as root:
            write_json(Path(root) / 'startup-error.json', {'http_status': 422, 'validation_fields': caught.exception.validation_fields})
            receipt = diagnose(root)
            self.assertFalse(receipt['can_retry'])
            self.assertEqual('fix_request', receipt['recovery'])
            self.assertNotIn('private-value', json.dumps(receipt))

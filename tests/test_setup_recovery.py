import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

from tests import _bootstrap
from tests import test_conversational_setup as fixtures
from amca.setup_node import connect, diagnose, write_json
from amca.transport import UrllibJsonHttpClient, JsonHttpError

configuration = fixtures.configuration


class RecoveryTests(unittest.TestCase):
    def test_minimal_confirm_each_configuration_needs_no_member_ids(self):
        from amca.setup_config import contract, validate_config
        value = contract()['template']
        value['space_name'] = '测试'
        value['card']['role_name'] = '我的助手'
        validate_config(value)
        self.assertEqual([], value['policy']['allowed_asker_ids'])
        self.assertEqual('confirm_each', value['policy']['confirmation_mode'])
        self.assertIsNone(value['policy']['answer_quota']['limit'])
        self.assertEqual('未声明', value['card']['owner_claim'])

    def test_network_permission_failure_is_provably_unsent(self):
        with patch('amca.transport.urlopen', side_effect=URLError(PermissionError(10013, 'private error body'))):
            with self.assertRaises(JsonHttpError) as caught:
                UrllibJsonHttpClient().request_json('POST', 'https://example.test', payload={})
        self.assertTrue(caught.exception.request_not_sent)
        self.assertEqual('network_permission_denied', caught.exception.failure_kind)
        self.assertNotIn('private error body', str(caught.exception))

    def test_timeout_must_remain_uncertain(self):
        with patch('amca.transport.urlopen', side_effect=URLError(TimeoutError())):
            with self.assertRaises(JsonHttpError) as caught:
                UrllibJsonHttpClient().request_json('POST', 'https://example.test', payload={})
        self.assertFalse(caught.exception.request_not_sent)

    def test_unsent_registration_can_retry_same_configuration(self):
        adapter = fixtures.SetupConnectionTests().adapter()
        good = adapter.register_agent.return_value
        adapter.register_agent.side_effect = [JsonHttpError(None, 'blocked', request_not_sent=True), good]
        with tempfile.TemporaryDirectory() as root, patch('amca.setup_node.AcnNetworkAdapter', return_value=adapter):
            with self.assertRaises(JsonHttpError):
                connect(configuration(), root, 'https://example.test/a2a')
            self.assertFalse((Path(root) / 'pending-network-operation.json').exists())
            identity, _ = connect(configuration(), root, 'https://example.test/a2a')
            self.assertEqual('setup-space', identity.subnet_slug)
            adapter.create_network.assert_called_once()

    def test_unsent_space_creation_reuses_identity(self):
        adapter = fixtures.SetupConnectionTests().adapter()
        adapter.create_network.side_effect = [JsonHttpError(None, 'blocked', request_not_sent=True), {'slug': 'setup-space'}]
        with tempfile.TemporaryDirectory() as root, patch('amca.setup_node.AcnNetworkAdapter', return_value=adapter):
            with self.assertRaises(JsonHttpError):
                connect(configuration(), root, 'https://example.test/a2a')
            connect(configuration(), root, 'https://example.test/a2a')
            adapter.register_agent.assert_called_once()

    def test_saved_result_recovers_crash_without_remote_recreation(self):
        adapter = fixtures.SetupConnectionTests().adapter()
        with tempfile.TemporaryDirectory() as root, patch('amca.setup_node.AcnNetworkAdapter', return_value=adapter):
            connect(configuration(), root, 'https://example.test/a2a')
            write_json(Path(root) / 'pending-network-operation.json', {'operation': 'create_space'})
            self.assertEqual('resume_saved_result', diagnose(root)['recovery'])
            connect(configuration(), root, 'https://example.test/a2a')
            adapter.register_agent.assert_called_once()
            adapter.create_network.assert_called_once()

    def test_old_unknown_record_is_preserved_without_retry(self):
        with tempfile.TemporaryDirectory() as root, patch('amca.setup_node.AcnNetworkAdapter') as adapter:
            path = Path(root) / 'pending-network-operation.json'
            write_json(path, {'operation': 'register'})
            before = path.read_bytes()
            self.assertEqual('remote_result_unknown', diagnose(root)['recovery'])
            with self.assertRaises(RuntimeError):
                connect(configuration(), root, 'https://example.test/a2a')
            self.assertEqual(before, path.read_bytes())
            adapter.assert_not_called()

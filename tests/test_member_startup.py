import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from tests import _bootstrap
from amca.member_node import run_member
from amca.transport import AgentIdentityReceipt


class MemberStartupTests(unittest.TestCase):
    def test_first_start_and_restart_keep_identity_and_start_real_receiver(self):
        adapter = Mock()
        identity = AgentIdentityReceipt('member-test', 'test-secret')
        adapter.register_agent.return_value = identity
        adapter.update_endpoint.return_value = {'endpoint_reachable': True, 'a2a_handshake_ok': True}
        adapter.discover.return_value = [{'agent_id': 'member-test'}]
        with tempfile.TemporaryDirectory() as directory, patch(
            'amca.pilot.AcnNetworkAdapter', return_value=adapter
        ), patch('amca.member_node.QuickTunnel.start') as tunnel_start, patch(
            'amca.pilot.join_from_invite', return_value=(identity, {'subnet_slug': 'space-test'})
        ) as join:
            for index in range(2):
                tunnel_start.return_value = Mock(url=f'https://endpoint-{index}.example')
                state = run_member(invite_code='test-code', cloudflared='unused',
                                   data_directory=directory, name='Test Member', owner_claim='Test')
                try:
                    url = f"http://127.0.0.1:{state['server'].server_port}"
                    with urlopen(url + '/health', timeout=5) as response:
                        self.assertEqual('member-test', json.load(response)['agent_id'])
                    with urlopen(url + '/', timeout=5) as response:
                        self.assertIn(b'Private agent chat', response.read())
                    policy = state['receiver'].profile.inbound_policy
                    self.assertEqual('confirm_each', policy.confirmation_mode.value)
                    self.assertEqual(frozenset(), policy.allowed_topics)
                    self.assertEqual('joined' if index == 0 else 'resumed', state['status'])
                finally:
                    state['server'].shutdown()
                    state['server'].server_close()
                    state['receiver'].close()
                    state['tunnel'].close()
            adapter.register_agent.assert_called_once()
            join.assert_called_once()
            adapter.update_endpoint.assert_called_once_with('https://endpoint-1.example/a2a')


if __name__ == '__main__':
    unittest.main()

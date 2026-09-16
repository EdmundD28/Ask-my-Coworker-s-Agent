from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from tests import _bootstrap  # noqa: F401

from amca.credentials import AcnCredentialBundle, WindowsCredentialStore


@unittest.skipUnless(os.name == "nt", "Windows DPAPI test")
class WindowsCredentialStoreTests(unittest.TestCase):
    def test_roundtrip_is_current_user_encrypted_and_not_plaintext(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "credentials.json"
            store = WindowsCredentialStore(path)
            original = AcnCredentialBundle(
                base_url="https://acn.example/api/v1",
                agent_id="agent-owner",
                api_key="do-not-store-this-in-plaintext",
                a2a_endpoint="https://owner.example/a2a",
                subnet_slug="private-space",
            )

            store.save(original)

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn(original.api_key, raw)
            self.assertIn("windows-dpapi-current-user", raw)
            self.assertEqual(original, store.load())

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tests import _bootstrap  # noqa: F401

from amca.invite_package import build_invite_package, repair_invite_package


class InvitePackageTests(unittest.TestCase):
    def test_builds_one_archive_with_executable_invite_and_launcher(self):
        with tempfile.TemporaryDirectory() as root:
            root_path = Path(root)
            executable = root_path / "member.exe"
            executable.write_bytes(b"fake executable")
            with patch(
                "amca.invite_package.create_one_time_invite",
                return_value={
                    "invite_code": "one-time-code",
                    "expires_at": "2026-09-17T00:00:00+00:00",
                },
            ):
                receipt = build_invite_package(
                    member_executable=executable,
                    output_directory=root_path / "out",
                    credential_path="owner.json",
                    coordinator_database="coordinator.db",
                    join_url="https://owner.example/onboarding/join",
                )

            archive = Path(receipt["package"])
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as bundle:
                names = set(bundle.namelist())
                self.assertEqual(
                    {
                        "AMCA-member-invitation/AMCA-member.exe",
                        "AMCA-member-invitation/invite.json",
                        "AMCA-member-invitation/Start-AMCA.cmd",
                    },
                    names,
                )
                invitation = json.loads(
                    bundle.read("AMCA-member-invitation/invite.json").decode("utf-8")
                )
                self.assertEqual("one-time-code", invitation["invite_code"])
                self.assertNotIn("api_key", invitation)
                launcher = bundle.read(
                    "AMCA-member-invitation/Start-AMCA.cmd"
                )
                launcher.decode("ascii")
                self.assertFalse(launcher.startswith(b"\xef\xbb\xbf"))

            executable.write_bytes(b"fixed executable")
            repair_invite_package(archive, member_executable=executable)
            with zipfile.ZipFile(archive) as repaired:
                invitation = json.loads(
                    repaired.read("AMCA-member-invitation/invite.json").decode("utf-8")
                )
                self.assertEqual("one-time-code", invitation["invite_code"])


if __name__ == "__main__":
    unittest.main()

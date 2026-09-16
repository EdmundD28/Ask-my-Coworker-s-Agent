from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

from .pilot import DEFAULT_ACN_BASE_URL, create_one_time_invite


LAUNCHER = '''@echo off
setlocal
echo AMCA launcher v2
if not exist "%~dp0AMCA-member.exe" goto missing_exe
if not exist "%~dp0invite.json" goto missing_invite
"%~dp0AMCA-member.exe" --invite-file "%~dp0invite.json" %*
set "amca_exit=%errorlevel%"
if not "%amca_exit%"=="0" echo AMCA exited with code %amca_exit%.
pause
exit /b %amca_exit%
:missing_exe
echo ERROR: AMCA-member.exe is missing from this folder.
echo Extract ALL files from the ZIP before running Start-AMCA.cmd.
echo If the EXE disappears after extraction, check antivirus quarantine history.
echo Do not disable antivirus protection.
echo Folder: "%~dp0"
dir /b "%~dp0"
pause
exit /b 2
:missing_invite
echo ERROR: invite.json is missing. Extract ALL files from the invitation ZIP.
pause
exit /b 3
'''


def _write_archive(
    *,
    archive: Path,
    executable: Path,
    invitation_manifest: dict,
) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        staging = Path(temporary) / "AMCA-member-invitation"
        staging.mkdir()
        shutil.copy2(executable, staging / "AMCA-member.exe")
        (staging / "invite.json").write_text(
            json.dumps(invitation_manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # Keep the complete cmd launch path ASCII-only. cmd.exe otherwise
        # decodes UTF-8 filenames using the machine's legacy code page.
        (staging / "Start-AMCA.cmd").write_text(
            LAUNCHER,
            encoding="ascii",
            newline="\r\n",
        )
        temporary_archive = archive.with_suffix(".zip.tmp")
        with zipfile.ZipFile(temporary_archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for item in staging.iterdir():
                bundle.write(item, arcname=f"AMCA-member-invitation/{item.name}")
        temporary_archive.replace(archive)


def build_invite_package(
    *,
    member_executable: str | Path,
    output_directory: str | Path,
    credential_path: str | Path,
    coordinator_database: str | Path,
    join_url: str,
    lifetime_hours: int = 24,
) -> dict:
    executable = Path(member_executable)
    if not executable.is_file():
        raise FileNotFoundError(f"member executable not found: {executable}")
    invitation = create_one_time_invite(
        credential_path,
        coordinator_database=coordinator_database,
        join_url=join_url,
        lifetime_hours=lifetime_hours,
    )
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "AMCA-member-invitation.zip"
    _write_archive(
        archive=archive,
        executable=executable,
        invitation_manifest={
            "schema": "amca.invite-package.v1",
            "invite_code": invitation["invite_code"],
            "expires_at": invitation["expires_at"],
            "base_url": DEFAULT_ACN_BASE_URL,
            "executable_sha256": digest,
        },
    )
    return {
        "package": str(archive.resolve()),
        "expires_at": invitation["expires_at"],
        "single_use": True,
        "executable_sha256": digest,
    }


def repair_invite_package(
    archive_path: str | Path,
    *,
    member_executable: str | Path,
) -> dict:
    """Repack an unused invitation without minting a second bearer token."""
    archive = Path(archive_path)
    executable = Path(member_executable)
    with zipfile.ZipFile(archive) as existing:
        manifest = json.loads(
            existing.read("AMCA-member-invitation/invite.json").decode("utf-8")
        )
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    manifest["executable_sha256"] = digest
    _write_archive(archive=archive, executable=executable, invitation_manifest=manifest)
    return {
        "package": str(archive.resolve()),
        "expires_at": manifest["expires_at"],
        "single_use": True,
        "executable_sha256": digest,
    }

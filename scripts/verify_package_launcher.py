"""Exercise the actual shipped CMD, without registering any agent."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile


def run(script, *args):
    command = '"' + os.environ.get('COMSPEC', 'cmd.exe') + '" /d /s /c ""' + str(script) + '" ' + ' '.join(args) + '"'
    result = subprocess.run(
        command,
        input=b'\r\n', capture_output=True, timeout=30,
        cwd=os.environ.get('SystemRoot', 'C:\\Windows'),
    )
    return result.returncode, result.stdout + result.stderr


archive = Path('dist/invitations/AMCA-member-invitation.zip')
with tempfile.TemporaryDirectory(prefix='AMCA launch check ') as directory:
    root = Path(directory) / '中文 空格 & folder'
    root.mkdir()
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(root)
    folder = root / 'AMCA-member-invitation'
    code, output = run(folder / 'Start-AMCA.cmd', '--help')
    assert code == 0 and b'Join AMCA in one step' in output, (code, output)
    missing = root / 'incomplete extraction'
    missing.mkdir()
    (missing / 'Start-AMCA.cmd').write_bytes((folder / 'Start-AMCA.cmd').read_bytes())
    code, output = run(missing / 'Start-AMCA.cmd')
    assert code == 2 and b'AMCA-member.exe is missing' in output, (code, output)
    print(json.dumps({'actual_cmd_help': 'passed', 'unicode_spaces_ampersand_path': 'passed',
                      'different_working_directory': 'passed', 'missing_exe_diagnostic': 'passed',
                      'network_registration_performed': False}))

"""Check the actual execution environment before claiming a one-time code."""
import secrets
import tempfile
from pathlib import Path

from .credentials import _protect, _unprotect
from .pilot import DEFAULT_ACN_BASE_URL
from .transport import UrllibJsonHttpClient, JsonHttpError


def preflight(code_file, registry):
    from .entry_hub import parse_entry
    packet = parse_entry(Path(code_file).read_text(encoding='utf-8-sig'))
    root = Path(packet['registry_path']) if packet['kind'] != 'join' else Path(registry)
    if packet['kind'] != 'join' and not (root / 'hub.sqlite3').is_file():
        raise ValueError('找不到接入码所属本机目录；请在本机界面重新生成')
    root.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryFile(dir=root) as handle:
            sample = secrets.token_bytes(32)
            handle.write(_protect(sample))
            handle.seek(0)
            if _unprotect(handle.read()) != sample:
                raise RuntimeError('本机凭据加密自检失败')
    except OSError as exc:
        raise PermissionError('当前工具环境不能写入或解密接入目录；请通过工具的正式权限机制选择可访问此目录的当前Windows账户，再执行同一命令') from exc
    try:
        UrllibJsonHttpClient().request_json('GET', DEFAULT_ACN_BASE_URL + '/agents')
    except JsonHttpError as exc:
        raise RuntimeError('接入前联网检查失败（' + str(exc.failure_kind or exc.status) + '）；请通过工具的正式权限机制取得联网权限后重试。尚未领取接入码、注册身份或创建空间。') from None
    return {'status': 'ready', 'directory_writable': True, 'credential_roundtrip': True,
            'service_reachable': True, 'entry_claimed': False, 'remote_identity_created': False,
            'next': '后续accept/apply/start/status/agent命令均使用本次通过检查的Windows账户与工具执行环境。'}

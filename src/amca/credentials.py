from __future__ import annotations

import base64
import ctypes
import json
import os
from ctypes import wintypes
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class AcnCredentialBundle:
    base_url: str
    agent_id: str
    api_key: str
    a2a_endpoint: str
    claim_url: str | None = None
    referral_url: str | None = None
    subnet_slug: str | None = None


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, ctypes.Array]:
    buffer = ctypes.create_string_buffer(data)
    return (
        _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("AMCA credential storage currently requires Windows DPAPI")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(source),
        "AMCA ACN credentials",
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("AMCA credential storage currently requires Windows DPAPI")
    source, source_buffer = _blob(data)
    output = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(source),
        None,
        None,
        None,
        None,
        0x1,
        ctypes.byref(output),
    )
    del source_buffer
    if not ok:
        raise PermissionError("当前Windows安全账户无法解密此凭据（或文件已损坏）；请使用创建凭据时同一账户及工具执行环境。不要删除凭据或重注册。")
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel32.LocalFree(output.pbData)


class WindowsCredentialStore:
    """Encrypt ACN credentials to the current Windows user with DPAPI."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def save(self, bundle: AcnCredentialBundle) -> None:
        plaintext = json.dumps(asdict(bundle), ensure_ascii=False).encode("utf-8")
        envelope = {
            "schema": "amca.acn-credentials.v1",
            "protection": "windows-dpapi-current-user",
            "ciphertext": base64.b64encode(_protect(plaintext)).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(json.dumps(envelope, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def load(self) -> AcnCredentialBundle:
        envelope = json.loads(self.path.read_text(encoding="utf-8"))
        if envelope.get("schema") != "amca.acn-credentials.v1":
            raise ValueError("unsupported AMCA credential schema")
        if envelope.get("protection") != "windows-dpapi-current-user":
            raise ValueError("unsupported AMCA credential protection")
        plaintext = _unprotect(base64.b64decode(envelope["ciphertext"]))
        return AcnCredentialBundle(**json.loads(plaintext.decode("utf-8")))

"""Per-Windows-user credentials, encrypted with DPAPI (never plaintext on disk)."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Optional, Tuple


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _crypt(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("Сохранение пароля доступно только в Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    target = _Blob()
    if decrypt:
        fn = crypt32.CryptUnprotectData
        second_type = ctypes.POINTER(wintypes.LPWSTR)
    else:
        fn = crypt32.CryptProtectData
        second_type = wintypes.LPCWSTR
    fn.argtypes = [ctypes.POINTER(_Blob), second_type, ctypes.POINTER(_Blob),
                   ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    fn.restype = wintypes.BOOL
    # CRYPTPROTECT_UI_FORBIDDEN; default scope is the current Windows user.
    if not fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.data, target.size)
    finally:
        kernel32.LocalFree(target.data)


class CredentialStore:
    def __init__(self, server: str, directory: Optional[Path] = None):
        base = directory or (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                             / "AuroraClient")
        self.path = base / (hashlib.sha256(server.encode("utf-8")).hexdigest() + ".credentials")

    def load(self) -> Optional[Tuple[str, str]]:
        try:
            encrypted = self.path.read_bytes()
        except FileNotFoundError:
            return None
        value = json.loads(_crypt(encrypted, decrypt=True).decode("utf-8"))
        if not isinstance(value, dict) or not all(isinstance(value.get(k), str) for k in ("login", "password")):
            raise ValueError("Некорректные сохранённые данные входа")
        return value["login"], value["password"]

    def save(self, login: str, password: str) -> None:
        encrypted = _crypt(json.dumps({"login": login, "password": password}, ensure_ascii=False).encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.path.parent, delete=False) as f:
                temporary = Path(f.name)
                f.write(encrypted)
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

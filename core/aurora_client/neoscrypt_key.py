from __future__ import annotations
import hashlib
import os
import struct
import subprocess
from pathlib import Path
PROFILE_WINDOWS = 2147485472
PROFILE_LINUX = 2147485216
CONST96_HEX = 'a92504c9708cd887ee5b94d8d6ca807f1618b09043ed970c21a66d9dcff173918771e2a127139edeb1309848153cf087e2d62c78212a9881b6f64dbefc2586b0a53168bfa2790fcb52f26ca40aaca1de49c7e468f090f31489e0c931fca9e31a'
CONST96 = bytes.fromhex(CONST96_HEX)
_DEFAULT_HARNESS = Path(__file__).resolve().parents[3] / 'reverse-engineering' / 'claude-reverse-engineering' / 'scripts' / 'neoscrypt_ref' / 'harness_plen.exe'

def _harness_path() -> Path:
    env = os.getenv('AURORA_NEOSCRYPT_HARNESS')
    return Path(env) if env else _DEFAULT_HARNESS

def derive_id_for_sign(login: str) -> int:
    s = login.strip()
    if s.lstrip('+-').isdigit():
        return int(s) & 4294967295
    return int.from_bytes(hashlib.sha256(s.encode('utf-8')).digest()[:4], 'big')

def compute_shash(password: str, challenge: str, gen: int=0) -> str:
    return hashlib.md5(f'{gen}_{password}_{challenge}'.encode('utf-8')).hexdigest()

def build_block(id_for_sign: int, s_hash: str, counter: int=0, size: int=256, with_const: bool=True) -> bytes:
    if len(s_hash) != 32:
        raise ValueError('sHash must be 32 hex chars (MD5)')
    head = struct.pack('<I', counter & 4294967295) + struct.pack('<I', id_for_sign & 4294967295) + s_hash.encode('ascii')
    if with_const:
        head = head + CONST96
    return head.ljust(size, b'\x00')

def _run_harness(block: bytes, profile: int, password_len: int) -> str:
    harness = _harness_path()
    if not harness.exists():
        raise FileNotFoundError(f'NeoScrypt harness not found: {harness}\nBuild it (scripts/neoscrypt_ref/BUILD.md) or set AURORA_NEOSCRYPT_HARNESS.')
    p = subprocess.run([str(harness), f'0x{profile:x}', str(password_len)], input=block.hex() + '\n', capture_output=True, text=True, timeout=60, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    out = (p.stdout or '').strip()
    if len(out) != 64:
        raise RuntimeError(f'harness failed: stdout={out!r} stderr={p.stderr!r}')
    return out.upper()

def compute_key(id_for_sign: int, password: str, challenge: str, gen: int=0, counter: int=0, profile: int=PROFILE_WINDOWS, password_len: int=256) -> str:
    s_hash = compute_shash(password, challenge, gen)
    block = build_block(id_for_sign, s_hash, counter=counter, size=password_len, with_const=True)
    return _run_harness(block, profile, password_len)

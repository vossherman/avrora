"""Bridge to the shared, verified core package `aurora_client` (new-client/first-client).

One core, one source of truth for crypto and protocol: we import it instead of copying it.
The location can be overridden with AURORA_CORE_DIR (a folder that contains `aurora_client/`).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
CORE_DIR = Path(os.getenv("AURORA_CORE_DIR") or (REPO_ROOT / "new-client" / "first-client"))

if not (CORE_DIR / "aurora_client" / "__init__.py").is_file():
    raise ImportError(f"aurora_client core not found in {CORE_DIR} (set AURORA_CORE_DIR)")
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from aurora_client import neoscrypt_key, protocol, storage  # noqa: E402
from aurora_client.config import Config  # noqa: E402
from aurora_client.exceptions import (  # noqa: E402
    AuroraError, AuthenticationError, ConnectionError, ProtocolError, ValidationError,
)

__all__ = [
    "CORE_DIR", "REPO_ROOT", "Config", "neoscrypt_key", "protocol", "storage",
    "AuroraError", "AuthenticationError", "ConnectionError", "ProtocolError", "ValidationError",
]

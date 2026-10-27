from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Optional


class UISettings:
    """Persist local UI preferences separately from account credentials."""

    MIN_SCALE = 80
    MAX_SCALE = 180
    DEFAULT_SCALE = 100

    def __init__(self, directory: Optional[Path] = None):
        base = directory or (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
                             / "AuroraClient")
        self.path = base / "ui-settings.json"

    def load_scale(self) -> int:
        try:
            with self.path.open("r", encoding="utf-8") as f:
                value = json.load(f)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return self.DEFAULT_SCALE
        if not isinstance(value, dict):
            return self.DEFAULT_SCALE
        scale = value.get("scale", self.DEFAULT_SCALE)
        if isinstance(scale, bool) or not isinstance(scale, int):
            return self.DEFAULT_SCALE
        if not self.MIN_SCALE <= scale <= self.MAX_SCALE:
            return self.DEFAULT_SCALE
        return scale

    def save_scale(self, percent: int) -> None:
        if isinstance(percent, bool) or not isinstance(percent, int):
            raise ValueError("Масштаб должен быть целым числом")
        if not self.MIN_SCALE <= percent <= self.MAX_SCALE:
            raise ValueError(f"Масштаб должен быть от {self.MIN_SCALE} до {self.MAX_SCALE}%")

        settings = {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            if isinstance(existing, dict):
                settings.update(existing)
        except (OSError, UnicodeError, json.JSONDecodeError):
            pass

        settings["scale"] = percent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             delete=False) as f:
                temporary = Path(f.name)
                json.dump(settings, f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(str(temporary), str(self.path))
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

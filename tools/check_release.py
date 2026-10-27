"""Offline self-check; no connection to the production service."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
report = {"release": "0.1", "ok": False}
session = server = None
try:
    manifest = ROOT / "SHA256SUMS.txt"
    if manifest.exists():
        verified = 0
        for line in manifest.read_text(encoding="utf-8").splitlines():
            expected, relative = line.split("  ", 1)
            file = (ROOT / relative).resolve()
            if not file.is_relative_to(ROOT) or not file.is_file():
                raise RuntimeError("Отсутствует файл комплекта: " + relative)
            digest = hashlib.sha256()
            with file.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(chunk)
            if digest.hexdigest() != expected:
                raise RuntimeError("Изменён или повреждён файл: " + relative)
            verified += 1
        report["verified_files"] = verified
    import tkinter
    import customtkinter
    import websockets
    from aurora_gui.core import Config, neoscrypt_key
    from aurora_gui.session import AuroraSession
    sys.path.insert(0, str(ROOT / "app" / "tests"))
    from mock_server import MockAurora, TEST_LOGIN, TEST_PASSWORD
    assert Path(sys.base_prefix).resolve() == (ROOT / "runtime").resolve(), "External Python runtime"
    for module in (customtkinter, websockets):
        assert Path(module.__file__).resolve().is_relative_to(ROOT), "External dependency"
    window = tkinter.Tk()
    window.withdraw()
    window.update_idletasks()
    window.destroy()
    signature = neoscrypt_key.compute_key(123, "local-check", "0.12345")
    assert len(signature) == 64, "Native helper failed"
    server = MockAurora()
    cfg = Config(ws_url=server.url, origin=f"http://127.0.0.1:{server.port}",
                 transport="qtwebengine", timeout=15)
    session = AuroraSession(cfg)
    session.login(TEST_LOGIN, TEST_PASSWORD)
    assert session.is_authorized and len(session.tasks) == 2, "Mock login failed"
    report.update(ok=True, python=sys.version.split()[0], qt=session._qt_engine.engine_info,
                  mock_tasks=len(session.tasks), native_helper="OK")
except Exception as error:
    report.update(error=f"{type(error).__name__}: {error}")
finally:
    if session is not None:
        session.close()
    if server is not None:
        server.drop_all()
        server.stop()

text = json.dumps(report, ensure_ascii=False, indent=2)
directory = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AuroraClient"
directory.mkdir(parents=True, exist_ok=True)
path = directory / "release-v01-check.txt"
path.write_text(text + "\n", encoding="utf-8")
if "--quiet" not in sys.argv:
    message = ("Проверка пройдена: целостность файлов, Python, интерфейс, подпись, QtWebEngine и мок-сервер."
               if report["ok"] else "Проверка не пройдена: " + report.get("error", "неизвестная ошибка"))
    ctypes.windll.user32.MessageBoxW(None, message + "\n\nОтчёт: " + str(path),
                                    "Аврора v0.1 — проверка", 0x40 if report["ok"] else 0x10)
elif sys.stdout is not None:
    print(text)
raise SystemExit(0 if report["ok"] else 1)

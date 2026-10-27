"""Windowed entry point used by the two double-click launchers."""
import argparse
import os
from pathlib import Path
import sys
import tempfile


def run_mock():
    from aurora_gui.core import Config
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tests"))
    from mock_server import MockAurora, TEST_LOGIN, TEST_PASSWORD
    from aurora_gui.app import AuroraApp
    from aurora_gui.settings import UISettings
    ui_settings = UISettings()

    # Fiddler/system proxies must not intercept the local demo server.
    os.environ["NO_PROXY"] = ",".join(filter(None, [os.environ.get("NO_PROXY", ""),
                                                  "127.0.0.1", "localhost"]))
    server = MockAurora()
    try:
        # Demo credentials are ephemeral and never replace the real account's store.
        with tempfile.TemporaryDirectory(prefix="aurora-demo-") as profile:
            os.environ["LOCALAPPDATA"] = profile
            origin = server.url.replace("ws://", "http://", 1).split("/", 3)[:3]
            app = AuroraApp(Config(ws_url=server.url, login=TEST_LOGIN,
                                   origin="/".join(origin), transport="qtwebengine"),
                            ui_settings=ui_settings)
            app.title("Аврора — ТЕСТОВЫЙ клиент (локальный мок-сервер)")
            app.pass_entry.insert(0, TEST_PASSWORD)
            try:
                app.mainloop()
            finally:
                if app.session:
                    app.session.close()
    finally:
        server.drop_all()
        server.stop()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    # pythonw doesn't always provide stdout/stderr. Logging uses the GUI journal.
    with open(os.devnull, "w") as sink:
        if sys.stdout is None:
            sys.stdout = sink
        if sys.stderr is None:
            sys.stderr = sink
        try:
            if args.mock:
                run_mock()
            else:
                from aurora_gui.app import main as run_gui
                run_gui()
        except Exception as error:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(error), "Аврора — ошибка запуска", 0x10)


if __name__ == "__main__":
    main()

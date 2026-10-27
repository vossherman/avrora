"""Private stdio worker: native Chromium WebSocket, no remote page or visible window.

Only the controlled in-memory bridge document runs JavaScript. Application payloads
travel over inherited pipes and QWebChannel, never command-line arguments or logs.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from urllib.parse import urlsplit


def output(event):
    sys.stdout.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    first = json.loads(sys.stdin.readline())
    origin = first.get("origin", "")
    parsed = urlsplit(origin)
    if (first.get("op") != "init" or parsed.scheme not in ("http", "https")
            or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("Invalid browser origin")

    # Windowless operation; no disabling Chromium's sandbox or TLS validation.
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import (QCoreApplication, QFile, QIODevice, QObject, QTimer,
                                Qt, QUrl, Signal, Slot, qVersion)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile,
                                         QWebEngineUrlRequestInfo, QWebEngineUrlRequestInterceptor,
                                         qWebEngineChromiumVersion)

    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QApplication(["aurora-network"])
    app.setQuitOnLastWindowClosed(False)

    class Bridge(QObject):
        command = Signal(str)
        incoming = Signal(object)

        def __init__(self):
            super().__init__()
            self.shutting_down = False
            self.incoming.connect(self.control)

        @Slot(str)
        def post(self, raw):
            try:
                event = json.loads(raw)
                if event.get("event") == "ready":
                    event.update(qt_version=qVersion(), chromium_version=qWebEngineChromiumVersion())
                output(event)
                if event.get("event") == "close" and self.shutting_down:
                    app.quit()
            except (ValueError, OSError):
                app.quit()

        @Slot(object)
        def control(self, command):
            if command.get("op") == "shutdown":
                self.shutting_down = True
                self.command.emit(json.dumps(command))
                QTimer.singleShot(1000, app.quit)
            elif command.get("op") == "invalid":
                output({"event": "fatal", "message": "Invalid browser IPC command"})
                app.quit()
            else:
                self.command.emit(json.dumps(command, ensure_ascii=False))

    bridge = Bridge()
    profile = QWebEngineProfile(app)  # off the record; kept for this host's lifetime
    profile.setHttpUserAgent(first.get("user_agent", profile.httpUserAgent()))

    class OnlySocketRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            if info.resourceType() == QWebEngineUrlRequestInfo.ResourceType.ResourceTypeWebSocket:
                return
            if info.requestUrl().scheme() not in ("data", "qrc"):
                # A base URL must not cause favicon or page-resource requests.
                info.block(True)

    interceptor = OnlySocketRequests(profile)
    profile.setUrlRequestInterceptor(interceptor)
    page = QWebEnginePage(profile, app)
    channel = QWebChannel(page)
    channel.registerObject("bridge", bridge)
    page.setWebChannel(channel)
    page.renderProcessTerminated.connect(lambda *_: (
        output({"event": "fatal", "message": "Browser renderer stopped"}), app.quit()))

    resource = QFile(":/qtwebchannel/qwebchannel.js")
    if not resource.open(QIODevice.OpenModeFlag.ReadOnly):
        raise RuntimeError("Qt WebChannel resource unavailable")
    channel_js = bytes(resource.readAll()).decode("utf-8")
    resource.close()
    expected_origin = json.dumps(origin, ensure_ascii=True).replace("<", "\\u003c")
    script = r"""
new QWebChannel(qt.webChannelTransport, function(channel) {
    const bridge = channel.objects.bridge;
    let socket = null;
    let active = 0;
    const post = value => bridge.post(JSON.stringify(value));
    if (location.origin !== new URL(EXPECTED_ORIGIN).origin) {
        post({event: 'fatal', message: 'Browser origin mismatch'});
        return;
    }
    bridge.command.connect(function(raw) {
        const cmd = JSON.parse(raw);
        if (cmd.op === 'open') {
            if (socket && socket.readyState < WebSocket.CLOSING) {
                post({event: 'error', id: cmd.id, message: 'Socket already open'});
                return;
            }
            const id = cmd.id;
            active = id;
            try {
                const ws = new WebSocket(cmd.url);
                socket = ws;
                ws.binaryType = 'arraybuffer';
                ws.onopen = () => post({event: 'open', id});
                ws.onmessage = function(event) {
                    const text = typeof event.data === 'string' ? event.data :
                        new TextDecoder('utf-8').decode(event.data);
                    post({event: 'message', id, text});
                };
                ws.onerror = () => post({event: 'error', id, message: 'WebSocket connection failed'});
                ws.onclose = event => post({event: 'close', id, code: event.code});
            } catch (_) {
                post({event: 'error', id, message: 'Cannot open WebSocket'});
            }
        } else if (cmd.op === 'send') {
            try {
                if (cmd.id !== active || !socket || socket.readyState !== WebSocket.OPEN)
                    throw new Error();
                socket.send(cmd.text);
                post({event: 'sent', id: cmd.id, seq: cmd.seq});
            } catch (_) {
                post({event: 'send_error', id: cmd.id, seq: cmd.seq, message: 'WebSocket send failed'});
            }
        } else if (cmd.op === 'close') {
            if (cmd.id === active && socket && socket.readyState < WebSocket.CLOSING)
                socket.close(1000);
            else
                post({event: 'close', id: cmd.id, code: 1000});
        } else if (cmd.op === 'shutdown') {
            if (socket && socket.readyState < WebSocket.CLOSING) socket.close(1000);
            else post({event: 'close', id: active, code: 1000});
        }
    });
    post({event: 'ready', origin: location.origin});
});
""".replace("EXPECTED_ORIGIN", expected_origin)
    # baseUrl sets the document's network origin without fetching the remote site.
    # No native original-client scripts or extra application requests are loaded.
    html = ("<!doctype html><html><head><meta charset='utf-8'></head><body><script>"
            + channel_js + "\n" + script + "</script></body></html>")
    page.setHtml(html, QUrl(origin.rstrip("/") + "/"))

    def read_commands():
        try:
            for line in sys.stdin:
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError()
                except ValueError:
                    bridge.incoming.emit({"op": "invalid"})
                    return
                bridge.incoming.emit(value)
        finally:
            bridge.incoming.emit({"op": "shutdown"})

    threading.Thread(target=read_commands, daemon=True, name="aurora-browser-input").start()
    try:
        return app.exec()
    finally:
        page.deleteLater()
        app.processEvents()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Never serialize exception messages that might contain IPC payloads.
        output({"event": "fatal", "message": "QtWebEngine worker could not start"})
        sys.exit(1)

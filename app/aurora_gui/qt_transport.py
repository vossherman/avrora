"""Thread-safe Python IPC client for the persistent Qt WebEngine WebSocket worker."""
from __future__ import annotations

import importlib.util
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger("aurora_gui.qt_transport")
MAX_MESSAGE_SIZE = 64 * 1024 * 1024


def _python_executable() -> str:
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


class _Connection:
    def __init__(self, host: "QtWebEngineTransport", ident: int):
        self._host = host
        self._id = ident
        self._messages: queue.Queue[object] = queue.Queue()
        self._closed = threading.Event()
        self._send_lock = threading.Lock()

    def send(self, text: str) -> None:
        if not isinstance(text, str):
            raise TypeError("WebSocket frames must be text")
        if len(text.encode("utf-8")) > MAX_MESSAGE_SIZE:
            raise ValueError("WebSocket frame exceeds maximum size")
        with self._send_lock:
            self._host._send_text(self, text)

    def recv(self) -> str:
        # Match websockets.sync.client.recv(): idle time is not a connection failure.
        # The worker/connection lifecycle always puts a terminal error to wake waiters.
        if self._closed.is_set() and self._messages.empty():
            raise OSError("WebSocket closed")
        item = self._messages.get()
        if isinstance(item, BaseException):
            raise item
        return item  # type: ignore[return-value]

    def close(self) -> None:
        self._host._close_connection(self)

    def _deliver(self, item: object) -> None:
        if not self._closed.is_set():
            self._messages.put(item)

    def _finish(self, error: Optional[BaseException] = None) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._messages.put(error or ConnectionError("WebSocket closed"))


class QtWebEngineTransport:
    """Own one persistent hidden Qt worker and multiplex its WebSockets by connection ID.

    The returned object implements the blocking send/recv/close API used by AuroraSession.
    The helper process and its Qt profile live across individual WebSocket reconnects.
    """

    def __init__(self, origin: str, user_agent: str, timeout: float = 30):
        self.origin = origin
        self.user_agent = user_agent
        self.timeout = float(timeout)
        self.engine_info: dict[str, str] = {}
        self._lock = threading.RLock()
        self._startup_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._process = None
        self._reader: Optional[threading.Thread] = None
        self._connections: dict[int, _Connection] = {}
        self._acks: dict[tuple[int, int], threading.Event] = {}
        self._ack_errors: dict[tuple[int, int], BaseException] = {}
        self._open_waiters: dict[int, queue.Queue[dict]] = {}
        self._ready = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._next_id = 0
        self._next_seq = 0
        self._closed = False
        self._failed = False

    @property
    def failed(self) -> bool:
        with self._lock:
            return self._failed

    def connect(self, url: str, timeout: Optional[float] = None) -> _Connection:
        limit = self.timeout if timeout is None else float(timeout)
        with self._startup_lock:
            self._ensure_worker(limit)
        with self._lock:
            if self._closed:
                raise OSError("Qt WebEngine transport is closed")
            ident = self._next_id
            self._next_id += 1
            conn = _Connection(self, ident)
            self._connections[ident] = conn
            open_result: queue.Queue[dict] = queue.Queue(maxsize=1)
            self._open_waiters[ident] = open_result
            try:
                self._write({"op": "open", "id": ident, "url": url})
            except Exception:
                self._connections.pop(ident, None)
                self._open_waiters.pop(ident, None)
                conn._finish(OSError("Qt WebEngine worker is unavailable"))
                raise
        # The worker acknowledges the open only after WebSocket.onopen.
        try:
            event = open_result.get(timeout=limit)
        except queue.Empty:
            event = None
        finally:
            with self._lock:
                self._open_waiters.pop(ident, None)
        if event is None:
            try:
                self._write({"op": "close", "id": ident})
            except Exception:
                pass
            self._forget_connection(conn)
            conn._finish(TimeoutError("WebSocket connection timed out"))
            raise TimeoutError("WebSocket connection timed out")
        if event.get("event") != "open":
            self._forget_connection(conn)
            conn._finish(OSError("Qt WebEngine WebSocket could not connect"))
            raise OSError("Qt WebEngine WebSocket could not connect")
        return conn

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            proc = self._process
            connections = list(self._connections.values())
            self._connections.clear()
            open_waiters = list(self._open_waiters.values())
            self._open_waiters.clear()
            if not self._ready.is_set():
                self._startup_error = OSError("Qt WebEngine transport closed")
                self._ready.set()
        for waiter in open_waiters:
            try:
                waiter.put_nowait({"event": "error"})
            except queue.Full:
                pass
        for conn in connections:
            conn._finish()
        self._fail_acks(OSError("Qt WebEngine transport closed"))
        if proc is None:
            return
        try:
            if proc.poll() is None:
                self._write({"op": "shutdown"})
                proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        finally:
            # Let the child exit so its stdout reader sees EOF before closing the pipe.
            try:
                proc.wait(timeout=1)
            except Exception:
                pass
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass
            reader = self._reader
            if reader is not None and reader is not threading.current_thread():
                reader.join(timeout=1)

    def _ensure_worker(self, timeout: float) -> None:
        with self._lock:
            proc = self._process
            if self._closed:
                raise OSError("Qt WebEngine transport is closed")
        if proc is not None:
            self._wait_ready(timeout)
            return
        if importlib.util.find_spec("PySide6") is None:
            raise OSError("PySide6 is required for Qt WebEngine transport")
        worker = Path(__file__).with_name("qt_worker.py")
        if not worker.is_file():
            raise OSError("Qt WebEngine worker file is missing")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        proc = subprocess.Popen(
            [_python_executable(), "-B", str(worker)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=flags,
        )
        with self._lock:
            if self._closed:
                proc.terminate()
                proc.wait(timeout=2)
                for stream in (proc.stdin, proc.stdout):
                    if stream:
                        stream.close()
                raise OSError("Qt WebEngine transport is closed")
            self._process = proc
            self._reader = threading.Thread(target=self._read_events, name="qt-ws-ipc-reader", daemon=True)
            self._reader.start()
        self._write({"op": "init", "origin": self.origin, "user_agent": self.user_agent})
        self._wait_ready(timeout)

    def _wait_ready(self, timeout: float) -> None:
        if not self._ready.wait(timeout):
            self._worker_failed("Qt WebEngine worker startup timed out")
            raise TimeoutError("Qt WebEngine worker startup timed out")
        if self._startup_error:
            raise OSError(str(self._startup_error))

    def _write(self, message: dict) -> None:
        proc = self._process
        if proc is None or proc.poll() is not None or proc.stdin is None:
            raise OSError("Qt WebEngine worker is not running")
        line = json.dumps(message, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()

    def _send_text(self, conn: _Connection, text: str) -> None:
        with self._lock:
            if conn._closed.is_set() or self._connections.get(conn._id) is not conn:
                raise OSError("WebSocket is closed")
            seq = self._next_seq
            self._next_seq += 1
            ack = threading.Event()
            key = (conn._id, seq)
            self._acks[key] = ack
        try:
            self._write({"op": "send", "id": conn._id, "seq": seq, "text": text})
            if not ack.wait(self.timeout):
                raise TimeoutError("WebSocket send timed out")
            with self._lock:
                error = self._ack_errors.pop(key, None)
            if error:
                raise error
        finally:
            with self._lock:
                self._acks.pop(key, None)
                self._ack_errors.pop(key, None)

    def _close_connection(self, conn: _Connection) -> None:
        with self._lock:
            if self._connections.get(conn._id) is not conn:
                conn._finish()
                return
        try:
            self._write({"op": "close", "id": conn._id})
        except Exception:
            pass
        # Browser close may not complete if the peer stalls; unblock recv promptly.
        conn._finish()
        self._forget_connection(conn)
        self._fail_conn_acks(conn._id, OSError("WebSocket closed"))

    def _forget_connection(self, conn: _Connection) -> None:
        with self._lock:
            self._connections.pop(conn._id, None)
        conn._finish()
        self._fail_conn_acks(conn._id, OSError("WebSocket closed"))

    def _read_events(self) -> None:
        proc = self._process
        try:
            if proc is None or proc.stdout is None:
                raise OSError("Qt WebEngine worker output is unavailable")
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("event is not an object")
                except Exception:
                    self._worker_failed("Qt WebEngine worker returned invalid IPC data")
                    return
                self._handle_event(event)
            self._worker_failed("Qt WebEngine worker exited")
        except Exception:
            self._worker_failed("Qt WebEngine worker communication failed")

    def _handle_event(self, event: dict) -> None:
        kind = event.get("event")
        if kind == "ready":
            self.engine_info = {
                "qt_version": str(event.get("qt_version", "")),
                "chromium_version": str(event.get("chromium_version", "")),
                "origin": str(event.get("origin", "")),
            }
            self._ready.set()
            return
        if kind == "fatal":
            self._worker_failed("Qt WebEngine worker failed to initialize")
            return
        if kind == "open_error":
            kind = "error"
        ident = event.get("id")
        if not isinstance(ident, int):
            return
        if kind in ("sent", "send_error"):
            seq = event.get("seq")
            if not isinstance(seq, int):
                return
            key = (ident, seq)
            with self._lock:
                ack = self._acks.get(key)
                if ack is None:
                    return
                if kind == "send_error":
                    self._ack_errors[key] = OSError("Qt WebEngine could not send WebSocket frame")
            if ack:
                ack.set()
            return
        with self._lock:
            waiter = self._open_waiters.get(ident)
            conn = self._connections.get(ident)
        if waiter is not None and kind in ("open", "error", "close"):
            try:
                waiter.put_nowait(event)
            except queue.Full:
                pass
            if kind == "open":
                return
            # onclose may follow onopen before connect() removes its waiter.
            # Its open result is already queued, but recv must still see closure.
        if conn is None:
            return
        if kind == "message":
            text = event.get("text")
            if isinstance(text, str) and len(text.encode("utf-8")) <= MAX_MESSAGE_SIZE:
                conn._deliver(text)
            else:
                conn._finish(OSError("Invalid or oversized WebSocket frame"))
                self._forget_connection(conn)
        elif kind in ("close", "error"):
            conn._finish(OSError("Qt WebEngine WebSocket closed"))
            self._forget_connection(conn)
            self._fail_conn_acks(ident, OSError("WebSocket closed"))

    def _fail_acks(self, error: BaseException) -> None:
        with self._lock:
            for key, ack in self._acks.items():
                self._ack_errors[key] = error
                ack.set()

    def _fail_conn_acks(self, ident: int, error: BaseException) -> None:
        with self._lock:
            for key, ack in self._acks.items():
                if key[0] == ident:
                    self._ack_errors[key] = error
                    ack.set()

    def _worker_failed(self, reason: str) -> None:
        with self._lock:
            if self._closed:
                return
            logger.warning("Qt WebEngine transport: %s", reason)
            self._failed = True
            self._startup_error = OSError(reason)
            self._ready.set()
            conns = list(self._connections.values())
            self._connections.clear()
            open_waiters = list(self._open_waiters.values())
            self._open_waiters.clear()
        for waiter in open_waiters:
            try:
                waiter.put_nowait({"event": "error"})
            except queue.Full:
                pass
        self._fail_acks(OSError("Qt WebEngine worker failed"))
        for conn in conns:
            conn._finish(OSError("Qt WebEngine worker failed"))


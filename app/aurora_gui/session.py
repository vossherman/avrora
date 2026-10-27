"""AuroraSession — one long-lived WebSocket per session, mimicking the original ARM_Student client.

Behaviour reference: clients/gui-client/PIPELINE.md. In short:
  * ONE socket for the whole session (ws.js `_util.websocket`), never a reconnect per command;
  * envelope {"data","ser_task","arm_task_id","v"}; arm_task_id is a single counter that keeps
    growing across reconnects (`_nextMsg++`), responses are matched by it, pushes (`_type:"msg"`)
    are dispatched by `mess_id`;
  * requests may be pipelined (several in flight), each one times out after 60 s;
  * on socket close: reconnect immediately if the socket had been open, otherwise back off
    100 ms / 1 s (after 10 failures) / 10 s (after 60 failures); after reconnect send
    getStudentArmVersion and re-authorize, then re-send the authorized requests that never got
    an answer (ws.js 'arm/authorized' handler);
  * after authorize: the same bootstrap sequence the original fires (pipeline.POST_LOGIN).

Secrets (password, key, authKey) live only in memory and are masked in every log line.
"""
from __future__ import annotations

import heapq
import itertools
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .core import AuthenticationError, Config, ConnectionError, ProtocolError, neoscrypt_key, protocol
from . import pipeline

logger = logging.getLogger("aurora_gui.session")

REQUEST_TIMEOUT = 60.0          # ws.js: 'Request timed out (60s)!'
TIMEOUT_ERROR = "Request timed out (60s)!"
MAX_MESSAGE_SIZE = 64 * 1024 * 1024   # task documents can be large; the browser has no 1 MB cap

# States reported to on_state
DISCONNECTED = "disconnected"
CONNECTING = "connecting"
CONNECTED = "connected"          # socket open, not authorized
AUTHORIZING = "authorizing"
READY = "ready"                  # authorized
RECONNECTING = "reconnecting"
CLOSED = "closed"


def _clean(raw: str) -> str:
    # ws.js onmessage: drop control chars except \t \n \r before JSON-decoding
    return "".join(c for c in raw if c in "\t\n\r" or ord(c) >= 32)


def _short(obj: Any, limit: int = 300) -> str:
    s = protocol.masked_json(obj)
    return s if len(s) <= limit else s[:limit] + f"...(+{len(s) - limit})"


@dataclass
class _Pending:
    msg_id: int
    ser_task: str
    raw: str
    authed: bool                      # sent while authorized -> re-sent after re-auth
    deadline: Optional[float]         # None = not sent yet (queued while offline)
    callback: Optional[Callable[[dict], None]] = None
    event: threading.Event = field(default_factory=threading.Event)
    response: Optional[dict] = None
    conn_gen: int = -1                # socket generation it was last sent on


class _Scheduler(threading.Thread):
    """One daemon thread for request timeouts and the original client's periodic timers."""

    def __init__(self):
        super().__init__(name="aurora-scheduler", daemon=True)
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq = itertools.count()
        self._cv = threading.Condition()
        self._stopped = False

    def call_later(self, delay: float, fn: Callable[[], None]):
        with self._cv:
            heapq.heappush(self._heap, (time.monotonic() + delay, next(self._seq), fn))
            self._cv.notify()

    def stop(self):
        with self._cv:
            self._stopped = True
            self._cv.notify()

    def run(self):
        while True:
            with self._cv:
                while not self._stopped and (not self._heap or self._heap[0][0] > time.monotonic()):
                    timeout = (self._heap[0][0] - time.monotonic()) if self._heap else None
                    self._cv.wait(timeout)
                if self._stopped:
                    return
                _, _, fn = heapq.heappop(self._heap)
            try:
                fn()
            except Exception:  # a timer must never kill the scheduler
                logger.exception("scheduled task failed")


class AuroraSession:
    """Persistent authorized session over the shared core (crypto: aurora_client.neoscrypt_key)."""

    def __init__(self, config: Optional[Config] = None, *,
                 on_state: Optional[Callable[[str], None]] = None,
                 on_push: Optional[Callable[[int, dict], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None,
                 on_notice: Optional[Callable[[str], None]] = None,
                 on_tasks: Optional[Callable[[List[dict]], None]] = None,
                 request_timeout: float = REQUEST_TIMEOUT,
                 ping_interval: Optional[float] = None,
                 run_bootstrap: bool = True,
                 input_time_origin: Optional[Tuple[float, float]] = None):
        self._synthetic_key_origin = (input_time_origin if input_time_origin is not None
                                      else (time.monotonic(), time.time() * 1000))
        self.cfg = config or Config.from_env()
        self.on_state = on_state
        self.on_push = on_push
        self.on_error = on_error
        self.on_notice = on_notice
        self.on_tasks = on_tasks
        self.request_timeout = request_timeout
        # Browsers (QtWebEngine) never send WS-level pings; keep that by default (None).
        self.ping_interval = ping_interval
        self.run_bootstrap = run_bootstrap

        self._lock = threading.RLock()
        self._send_lock = threading.RLock()
        self._connect_lock = threading.RLock()
        self._conn = None
        self._qt_engine = None
        self._conn_gen = 0
        self._auth_gen = 0                     # challenge/signature attempt on this socket
        self._next_msg = 0                       # ws.js _nextMsg: never reset for the page lifetime
        self._pending: Dict[int, _Pending] = {}
        self._ready = False                      # socket open
        self._is_auth = False                    # authorized on the current socket
        self._auth_event = threading.Event()
        self._closing = False
        self._fail_count = 0
        self._reconnecting = False
        self._state = DISCONNECTED
        self._sched = _Scheduler()
        self._sched.start()

        # Credentials for re-authorization, memory only. See PIPELINE.md §6 for the choice
        # (numeric id + latest authKey, gen=0 — the server-proven "remembered login" mode).
        self._reauth: Optional[Tuple[str, str]] = None
        self._login_creds: Optional[Tuple[str, str]] = None

        self.student: Optional[dict] = None
        self.must_change_password = False
        self.server_version: Optional[int] = None
        self.bootstrap: Dict[str, Any] = {}      # ser_task -> data from the post-login sequence
        self._bootstrap_complete = False
        self.tasks: List[dict] = []              # last getTaskList
        self._push_handlers: Dict[int, List[Callable[[dict], None]]] = {}
        self._timer_gen = 0                      # bumped on disconnect: clears setInterval timers
        pipeline.install_push_handlers(self)

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> str:
        return self._state

    @property
    def is_authorized(self) -> bool:
        return self._is_auth

    def _set_state(self, st: str):
        if st == self._state:
            return
        self._state = st
        logger.info(f"state: {st}")
        if self.on_state:
            try:
                self.on_state(st)
            except Exception:
                logger.exception("on_state callback failed")

    # ------------------------------------------------------------------ socket
    def connect(self):
        """Open the socket (blocking). Like the page load: sends getStudentArmVersion right away."""
        with self._connect_lock:
            self._connect()

    def _connect(self):
        with self._lock:
            if self._ready:
                return
            if self._closing:
                raise ConnectionError("Session closed")
        self._set_state(CONNECTING)
        try:
            self._establish_connection()
        except Exception:
            if not self._closing:
                self._fail_count += 1
                self._set_state(RECONNECTING)
                self._start_reconnect(immediate=False)
            raise

    def _establish_connection(self):
        # The GUI's startup connection and the login/reconnect worker can overlap.
        # Publish a socket and its initial version request as one connection step.
        with self._connect_lock:
            if self._ready or self._closing:
                return
            self._open_socket()
            self._on_open()

    def _open_socket(self):
        from .transport import backend_for
        backend = backend_for(self.cfg)
        if backend == "qtwebengine":
            from .qt_transport import QtWebEngineTransport
            engine = self._qt_engine
            if engine is None or engine.failed:
                if engine is not None:
                    engine.close()
                engine = QtWebEngineTransport(self.cfg.origin, self.cfg.user_agent,
                                              timeout=min(self.cfg.timeout, 30))
                with self._lock:
                    if self._closing:
                        raise ConnectionError("Session closed during connection")
                    self._qt_engine = engine
            conn = engine.connect(self.cfg.ws_url)
            info = engine.engine_info
            logger.info("network: QtWebEngine %s / Chromium %s",
                        info.get("qt_version"), info.get("chromium_version"))
        else:
            from websockets.sync.client import connect as ws_connect
            conn = ws_connect(
                self.cfg.ws_url,
                origin=self.cfg.origin,
                user_agent_header=self.cfg.user_agent,
                additional_headers={"Cache-Control": "no-cache", "Pragma": "no-cache",
                                    "Accept-Encoding": "gzip, deflate, br, zstd"},
                open_timeout=min(self.cfg.timeout, 30),
                ping_interval=self.ping_interval,
                ping_timeout=None if self.ping_interval is None else 20,
                max_size=MAX_MESSAGE_SIZE,
                close_timeout=5,
            )
        with self._lock:
            if self._closing:
                conn.close()
                raise ConnectionError("Session closed during connection")
            self._conn_gen += 1
            self._auth_gen = 0
            gen = self._conn_gen
            self._conn = conn
            self._ready = True
            self._is_auth = False
            self._auth_event.clear()
            self._fail_count = 0
        threading.Thread(target=self._reader_loop, args=(conn, gen),
                         name=f"aurora-reader-{gen}", daemon=True).start()
        logger.info(f"connected to {self.cfg.ws_url} (socket #{gen})")
        self._set_state(CONNECTED)

    def _on_open(self):
        # app.js asks the server version on every 'ws/connected'
        self.send("getStudentArmVersion", {}, callback=self._on_version, authed=False)

    def _on_version(self, rsp: dict):
        if rsp.get("success"):
            self.server_version = rsp.get("data")
            if self.server_version != self.cfg.protocol_version:
                msg = (f"Версия сервера {self.server_version} отличается от клиентской "
                       f"v={self.cfg.protocol_version}")
                logger.warning(msg)
                self._emit_error(msg)

    def close(self):
        """Explicit logout/quit: ws.js disconnect() -> no reconnect."""
        with self._lock:
            self._closing = True
            conn = self._conn
            self._timer_gen += 1
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        engine = self._qt_engine
        if engine is not None:
            engine.close()
        self._fail_all("Connection closed")
        with self._lock:
            self._conn = None
            self._ready = False
            self._is_auth = False
        self._reauth = None
        self._login_creds = None
        self._sched.stop()
        self._set_state(CLOSED)

    def _reader_loop(self, conn, gen: int):
        err = None
        try:
            while True:
                raw = conn.recv()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                self._dispatch(raw)
        except Exception as e:  # ConnectionClosed* or socket errors
            err = e
        self._handle_close(gen, err)

    def _dispatch(self, raw: str):
        try:
            msg = json.loads(_clean(raw))
        except json.JSONDecodeError:
            logger.warning(f"malformed frame ({len(raw)} chars) ignored")
            return
        if not isinstance(msg, dict):
            return
        if not msg.get("success") and "error" in msg:
            self._emit_error(str(msg.get("error")))
        kind = msg.get("_type")
        if kind == "rsp":
            mid = msg.get("arm_task_id")
            with self._lock:
                p = self._pending.pop(mid, None)
            logger.debug(f"<- rsp #{mid} success={msg.get('success')}")
            if p is not None:
                self._complete(p, msg)
        elif kind == "msg":
            mess_id = msg.get("mess_id")
            logger.info(f"<- push mess_id={mess_id} {_short(msg, 200)}")
            for h in list(self._push_handlers.get(mess_id, [])):
                try:
                    h(msg)
                except Exception:
                    logger.exception(f"push handler {mess_id} failed")
            if self.on_push:
                try:
                    self.on_push(mess_id, msg)
                except Exception:
                    logger.exception("on_push callback failed")
        else:
            logger.debug(f"<- unknown frame {_short(msg, 200)}")

    def _handle_close(self, gen: int, err: Optional[BaseException]):
        with self._lock:
            if gen != self._conn_gen:
                return                           # an older socket; already replaced
            was_ready = self._ready
            self._conn = None
            self._ready = False
            self._is_auth = False
            self._auth_event.clear()
            self._timer_gen += 1                 # 'ws/disconnected' clears the news interval
            closing = self._closing
            # unauthorized requests are not replayed by ws.js -> fail them now instead of
            # letting the caller sit out the full 60 s
            dead = [p for p in self._pending.values() if not p.authed]
            for p in dead:
                self._pending.pop(p.msg_id, None)
        for p in dead:
            self._complete(p, {"success": False, "error": "Connection lost!", "arm_task_id": p.msg_id})
        if closing:
            return
        logger.warning(f"socket #{gen} closed: {err!r}")
        self._set_state(RECONNECTING)
        self._start_reconnect(immediate=was_ready)

    def _backoff(self) -> float:
        # ws.js onclose: 100 ms, 1 s after 10 failures, 10 s after 60 failures
        if self._fail_count > 60:
            return 10.0
        if self._fail_count > 10:
            return 1.0
        return 0.1

    def _start_reconnect(self, immediate: bool):
        with self._lock:
            if self._reconnecting or self._closing:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, args=(immediate,),
                         name="aurora-reconnect", daemon=True).start()

    def _reconnect_loop(self, immediate: bool):
        try:
            delay = 0.0 if immediate else self._backoff()
            while not self._closing:
                if delay:
                    time.sleep(delay)
                if self._closing:
                    return
                try:
                    self._establish_connection()
                    break
                except Exception as e:
                    self._fail_count += 1
                    delay = self._backoff()
                    logger.info(f"reconnect failed ({self._fail_count}): {e}; retry in {delay}s")
            else:
                return
            if self._reauth:
                try:
                    self._authorize(*self._reauth)
                except Exception as e:
                    logger.error(f"re-authorization failed: {e}")
                    self._emit_error(f"Повторная авторизация не удалась: {e}")
                    self._try_password_reauth()
        finally:
            with self._lock:
                self._reconnecting = False
                retry = not self._closing and not self._ready
            # A second close during re-authorization couldn't start a worker while
            # this one was active. Hand it off now instead of leaving the session offline.
            if retry:
                self._start_reconnect(immediate=True)

    def _try_password_reauth(self):
        if not self._login_creds or self._closing or self._is_auth or not self._ready:
            return
        try:
            self._authorize(*self._login_creds)
        except Exception as e:
            logger.error(f"re-authorization with login/password failed: {e}")
            if self._ready and not self._closing:
                self._fail_all("Re-authorization failed")
                self._set_state(CONNECTED)

    # ------------------------------------------------------------------ requests
    def _build(self, ser_task: str, data: Any, msg_id: int) -> str:
        # Same key order and compact UTF-8 encoding as DS.JSON.encode in the captures
        env = {"data": data, "ser_task": ser_task, "arm_task_id": msg_id, "v": self.cfg.protocol_version}
        return json.dumps(env, ensure_ascii=False, separators=(",", ":"))

    def _transmit(self, p: _Pending) -> bool:
        with self._send_lock:
            with self._lock:
                conn, gen = self._conn, self._conn_gen
            if conn is None:
                return False
            try:
                conn.send(p.raw)
            except Exception as e:
                logger.warning(f"send #{p.msg_id} {p.ser_task} failed: {e}")
                return False
            p.conn_gen = gen
            self._arm_deadline(p)
            return True

    def send(self, ser_task: str, data: Any = None, callback: Optional[Callable[[dict], None]] = None,
             *, authed: Optional[bool] = None) -> int:
        """Asynchronous send (DS.armCmd). Returns arm_task_id; callback(rsp) is called from a
        background thread with the full response dict (or a synthesized timeout/failure)."""
        # JS allocates the id and sends the frame in one event-loop turn. Keep
        # that order across GUI, reader callbacks, push handlers and timer threads.
        with self._send_lock:
            return self._send(ser_task, data, callback, authed=authed)

    def _send(self, ser_task: str, data: Any, callback: Optional[Callable[[dict], None]],
              *, authed: Optional[bool]) -> int:
        if data is None:
            data = {}
        with self._lock:
            mid = self._next_msg
            self._next_msg += 1
            if authed is None:
                # ws.js files it under _sentCmds while authorized; while we are reconnecting with
                # stored credentials it is queued the same way and goes out after re-auth
                authed = self._is_auth or self._reauth is not None
            p = _Pending(mid, ser_task, self._build(ser_task, data, mid), authed, None, callback)
            self._pending[mid] = p
            sendable = self._ready and (self._is_auth or not authed)
        logger.info(f"-> #{mid} {ser_task} {_short(data, 160)}")
        if sendable and self._transmit(p):
            return mid
        if authed:
            self._arm_deadline(p)            # waits for (re)authorization, 60 s budget
            return mid
        with self._lock:
            self._pending.pop(mid, None)
        self._complete(p, {"success": False, "error": "Not connected", "arm_task_id": mid})
        return mid

    def _arm_deadline(self, p: _Pending):
        if p.deadline is None:
            p.deadline = time.monotonic() + self.request_timeout
            self._sched.call_later(self.request_timeout + 0.05, lambda: self._expire(p))

    def call(self, ser_task: str, data: Any = None, *, timeout: Optional[float] = None,
             authed: Optional[bool] = None) -> dict:
        """Blocking request; returns the full response dict (success may be False)."""
        box: Dict[str, dict] = {}
        done = threading.Event()

        def cb(rsp):
            box["rsp"] = rsp
            done.set()

        self.send(ser_task, data, cb, authed=authed)
        done.wait((timeout or self.request_timeout) + 1.0)
        return box.get("rsp") or {"success": False, "error": TIMEOUT_ERROR}

    def request(self, ser_task: str, data: Any = None, **kw) -> Any:
        """Blocking request; returns `data`, raises ProtocolError on success:false."""
        rsp = self.call(ser_task, data, **kw)
        if not rsp.get("success"):
            raise ProtocolError(f"{ser_task}: {rsp.get('error') or 'success=false'}")
        return rsp.get("data")

    def _complete(self, p: _Pending, rsp: dict):
        p.response = rsp
        p.event.set()
        if p.callback:
            try:
                p.callback(rsp)
            except Exception:
                logger.exception(f"callback for {p.ser_task} failed")

    def _expire(self, p: _Pending):
        with self._lock:
            if self._pending.get(p.msg_id) is not p:
                return
            if p.deadline is not None and time.monotonic() < p.deadline:
                remaining = p.deadline - time.monotonic()
                self._sched.call_later(remaining + 0.05, lambda: self._expire(p))
                return
            self._pending.pop(p.msg_id, None)
        logger.warning(f"#{p.msg_id} {p.ser_task}: {TIMEOUT_ERROR}")
        self._complete(p, {"success": False, "error": TIMEOUT_ERROR, "arm_task_id": p.msg_id})

    def _fail_all(self, error: str):
        with self._lock:
            items = list(self._pending.values())
            self._pending.clear()
        for p in items:
            self._complete(p, {"success": False, "error": error, "arm_task_id": p.msg_id})

    def _replay_authed(self):
        # ws.js 'arm/authorized': re-send every authorized command still waiting for an answer
        with self._lock:
            gen = self._conn_gen
            todo = sorted((p for p in self._pending.values() if p.authed and p.conn_gen != gen),
                          key=lambda p: p.msg_id)
        for p in todo:
            logger.info(f"replay #{p.msg_id} {p.ser_task}")
            self._transmit(p)

    def _emit_error(self, text: str):
        if self.on_error:
            try:
                self.on_error(text)
            except Exception:
                logger.exception("on_error callback failed")

    def add_push_handler(self, mess_id: int, fn: Callable[[dict], None]):
        self._push_handlers.setdefault(mess_id, []).append(fn)

    def notify(self, text: str):
        logger.info(text)
        if self.on_notice:
            try:
                self.on_notice(text)
            except Exception:
                logger.exception("on_notice callback failed")

    def tasks_updated(self):
        if self.on_tasks:
            try:
                self.on_tasks(list(self.tasks))
            except Exception:
                logger.exception("on_tasks callback failed")

    # timers (the original's setInterval), owned by the scheduler thread
    def schedule(self, delay: float, fn: Callable[[], None]):
        self._sched.call_later(delay, fn)

    def timer_token(self) -> int:
        return self._timer_gen

    def timer_alive(self, token: int) -> bool:
        return token == self._timer_gen and not self._closing

    # ------------------------------------------------------------------ auth
    def login(self, login: str, password: str) -> dict:
        """Interactive login (DS.ARM.authorize from the login form), then the post-login sequence."""
        login = login.strip()
        if not login or not password:
            raise AuthenticationError("Укажите логин и пароль")
        self.connect()
        rsp = self._authorize(login, password)
        self._login_creds = (login, password)
        return rsp

    def _authorize(self, user_id: str, password: str) -> dict:
        connection_gen = self._conn_gen
        self._check_auth_connection(connection_gen)
        self._set_state(AUTHORIZING)
        ch = self.call("newChallenge", {}, authed=False)
        self._check_auth_connection(connection_gen)
        if not ch.get("success"):
            self._set_state(CONNECTED)
            raise AuthenticationError(f"newChallenge: {ch.get('error') or 'success=false'}")
        challenge = str(ch.get("data"))
        # Original retry capture: failed password gen=0, next attempt gen=1.
        # Both the MD5 input and the native block counter advance together.
        with self._lock:
            self._check_auth_connection(connection_gen)
            gen = self._auth_gen
            self._auth_gen += 1
        idf = self.cfg.id_for_sign or neoscrypt_key.derive_id_for_sign(str(user_id))
        key = neoscrypt_key.compute_key(idf, password, challenge, gen=gen, counter=gen,
                                        profile=self.cfg.neoscrypt_profile,
                                        password_len=self.cfg.neoscrypt_password_len)
        rsp = self.call("authorize", {"user_id": user_id, "password": password, "key": key,
                                      "gen": gen, "g2a": 0}, authed=False)
        self._check_auth_connection(connection_gen)
        if not rsp.get("success"):
            self._set_state(CONNECTED)
            raise AuthenticationError(rsp.get("error") or "Сервер отклонил авторизацию")
        student = rsp.get("student") or {}
        first = not self._bootstrap_complete
        with self._lock:
            self._check_auth_connection(connection_gen)
            self.student = student
            self.must_change_password = bool(rsp.get("must_change_passw"))
            auth_key = rsp.get("authKey")
            if student.get("id") is not None and auth_key:
                self._reauth = (str(student["id"]), auth_key)
            self._is_auth = True
        logger.info(f"authorized as id={student.get('id')} group={student.get('group_name')}")
        self._replay_authed()
        try:
            self._after_authorized(first)
        except Exception:
            with self._lock:
                if connection_gen == self._conn_gen and self._ready and not self._closing:
                    self._is_auth = False
                    self._auth_event.clear()
                    self._set_state(CONNECTED)
            raise
        with self._lock:
            self._check_auth_connection(connection_gen)
            self._bootstrap_complete = True
            self._auth_event.set()
            self._set_state(READY)
        return rsp

    def _check_auth_connection(self, gen: int):
        if self._closing or not self._ready or gen != self._conn_gen:
            raise AuthenticationError("Connection changed during authorization")

    def _after_authorized(self, first: bool):
        if not self.run_bootstrap:
            return
        if first:
            pipeline.run_post_login(self)        # fnAuthorize: prefs, then 'arm/authorized'
        else:
            pipeline.on_authorized(self)         # re-auth fires only 'arm/authorized'

    # ------------------------------------------------------------------ task API
    def list_tasks(self) -> List[dict]:
        """module.js loadTasks: getTaskList -> getUnitsList, getStats in parallel."""
        return pipeline.load_tasks(self)

    def get_task(self, task_id: int) -> dict:
        data = self.request("getTask", int(task_id))
        if not isinstance(data, dict):
            raise ProtocolError(f"getTask {task_id}: unexpected data {type(data).__name__}")
        return data

    def get_task_files(self, task_id: int) -> List[dict]:
        data = self.request("getTaskFiles2", int(task_id))
        if data is None:
            return []
        if not isinstance(data, list):
            raise ProtocolError(f"getTaskFiles2 {task_id}: unexpected data {type(data).__name__}")
        return data

    def open_task(self, task_id: int) -> Tuple[dict, List[dict]]:
        """Open a task the way the original task module does (see pipeline.open_task)."""
        return pipeline.open_task(self, int(task_id))

    def save_task(self, task: dict) -> None:
        """saveTask with the full task document (read-modify-write, like the original)."""
        doc = dict(task)                         # same keys, same order as getTask
        # __lastSaved records the document edit; saving/closing does not touch it.
        self.request("saveTask", doc)

    def save_task_file(self, task_id: int, name: str, content: str) -> None:
        self.request("saveTaskFile", {"idTask": int(task_id), "name": name, "file": content})

"""Local fake Aurora server for offline tests (ws://127.0.0.1, fake credentials only).

It verifies the authorize key with the same core algorithm (so the crypto wiring is exercised),
answers the post-login sequence with capture-shaped data, stores saveTask/saveTaskFile in memory,
records every client frame, and can drop the socket or push `_type:"msg"` frames on demand.
"""
from __future__ import annotations

import json
import threading
import uuid
from typing import Any, Dict, List, Optional

from websockets.sync.server import serve

from aurora_gui.core import neoscrypt_key

TEST_LOGIN = "test.student@example.test"
TEST_PASSWORD = "not-a-real-password"
STUDENT = {"id": 4242, "student_name": "Тест", "student_suname": "Тестов", "student_patronymic": "Т",
           "group_id": 1, "use_external_auth": False, "group_name": "ТЕСТ-00-00"}


def _task(tid: int, name: str) -> dict:
    return {"id": tid, "teacher_task_id": 1, "status": 1, "task_delay": None,
            "tools": [{"id": 1, "status": 0}, {"id": 5, "status": 0}, {"id": 6, "status": 0}],
            "date_added": "1789674150", "type": 1,
            "score": None, "comment": "", "name": name, "parent_id": 0, "section_id": 7,
            "has_theory_text": False, "subject_id": 2, "language_id": 2, "date_control": "1",
            "isLocked": False}


class MockAurora:
    def __init__(self):
        self.received: List[dict] = []
        self.tasks: Dict[int, dict] = {
            101: dict(_task(101, "Задание 1_1 Тест"), __lastSaved=1789674150, method2={"main": {"objects": [], "functions": [],
                      "custom": []}, "classes": []}, algo2=[], algo_graph="", task_text="<p>Текст</p>",
                      algo_text="", method_description=""),
            102: dict(_task(102, "Задание 1_2 Тест"), method2={}, algo2=[], algo_graph="", task_text=""),
        }
        self.files: Dict[int, Dict[str, str]] = {101: {"main.cpp": "int main(){return 0;}\n"}, 102: {}}
        self.auth_keys: set = set()
        self._challenges: Dict[Any, str] = {}
        self._auth_generations: Dict[Any, int] = {}
        self._conns: list = []
        self._lock = threading.Lock()
        self.drop_next: Optional[str] = None     # ser_task: close the socket instead of answering
        self.server = serve(self._handler, "127.0.0.1", 0, max_size=None)
        self.port = self.server.socket.getsockname()[1]
        self.url = f"ws://127.0.0.1:{self.port}/student/arm/"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    # ------------------------------------------------------------ control
    def stop(self):
        self.server.shutdown()

    def drop_all(self):
        for c in list(self._conns):
            try:
                c.close()
            except Exception:
                pass

    def push(self, mess_id: int, **payload):
        msg = json.dumps(dict({"_type": "msg", "mess_id": mess_id}, **payload), ensure_ascii=False)
        for c in list(self._conns):
            c.send(msg)

    def tasks_sent(self) -> List[str]:
        return [m["ser_task"] for m in self.received]

    # ------------------------------------------------------------ protocol
    def _handler(self, conn):
        self._conns.append(conn)
        try:
            for raw in conn:
                req = json.loads(raw)
                with self._lock:
                    self.received.append(req)
                cmd, mid, data = req["ser_task"], req["arm_task_id"], req.get("data")
                if self.drop_next == cmd:
                    self.drop_next = None
                    conn.close()
                    return
                conn.send(json.dumps(self._answer(conn, cmd, mid, data), ensure_ascii=False))
        except Exception:
            pass
        finally:
            self._challenges.pop(id(conn), None)
            self._auth_generations.pop(id(conn), None)
            if conn in self._conns:
                self._conns.remove(conn)

    def _answer(self, conn, cmd: str, mid: int, data: Any) -> dict:
        ok = lambda d=None: {"success": True, "data": d, "arm_task_id": mid, "_type": "rsp"}
        fail = lambda err=None: dict({"success": False, "arm_task_id": mid, "_type": "rsp"},
                                     **({"error": err} if err else {}))
        if cmd == "getStudentArmVersion":
            return ok(213)
        if cmd == "newChallenge":
            ch = f"0.{uuid.uuid4().int % 10**17:017d}"
            self._challenges[id(conn)] = ch
            return ok(ch)
        if cmd == "authorize":
            return self._authorize(conn, mid, data)
        if cmd == "getTaskList":
            return ok([{k: v for k, v in t.items() if k in _task(0, "")} for t in self.tasks.values()])
        if cmd == "getTask":
            return ok(self.tasks[data]) if data in self.tasks else fail()
        if cmd == "getTaskFiles2":
            return ok([{"name": n, "file": f} for n, f in self.files.get(data, {}).items()])
        if cmd == "saveTask":
            self.tasks[data["id"]] = data
            return ok(None)
        if cmd == "saveTaskFile":
            self.files.setdefault(data["idTask"], {})[data["name"]] = data["file"]
            return {"success": True, "arm_task_id": mid, "_type": "rsp"}
        if cmd == "shouldSelectCourseWork":
            return ok([71])
        if cmd == "loadChatPrevN":
            return ok([{"rowid": 2, "date_added": "1789670000", "message_text": "hi"}])
        if cmd == "loadUserPreferences":
            return ok({"news/lastId": 85})
        if cmd in ("getActiveTest", "getCourseWorkOptions"):
            return fail()
        if cmd == "getTaskTestsValidity":
            return {"success": True, "arm_task_id": mid, "_type": "rsp"}
        return ok([] if cmd.startswith(("get", "load")) else None)

    def _authorize(self, conn, mid: int, d: dict) -> dict:
        ch = self._challenges.get(id(conn))
        gen = self._auth_generations.get(id(conn), 0)
        self._auth_generations[id(conn)] = gen + 1
        user, pwd = str(d.get("user_id")), d.get("password")
        valid = (user == TEST_LOGIN and pwd == TEST_PASSWORD) or \
                (user == str(STUDENT["id"]) and pwd in self.auth_keys)
        if ch and valid:
            idf = neoscrypt_key.derive_id_for_sign(user)
            expect = neoscrypt_key.compute_key(idf, pwd, ch, gen=gen, counter=gen)
            if d.get("gen") == gen and expect == d.get("key"):
                key = "{" + str(uuid.uuid4()).upper() + "}"
                self.auth_keys.add(key)
                return {"must_change_passw": False, "success": True, "student": STUDENT,
                        "authKey": key, "arm_task_id": mid, "_type": "rsp"}
            return {"success": False, "error": "Ваша версия системы устарела", "arm_task_id": mid, "_type": "rsp"}
        return {"success": False, "error": "Неверный пароль", "arm_task_id": mid, "_type": "rsp"}

"""The original client's call sequences (see PIPELINE.md), driven over an AuroraSession.

Sources: wire order/arguments from reverse-engineering/hand-data/WS.txt + ws-auth.txt; triggers and
dependencies from the student frontend (app.js, modules/task/module.js, jsons.txt).
Analysis notes: clients/gui-client/analysis/{captures-sequence,frontend-pipeline}.md.
"""
from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from .core import ProtocolError

if TYPE_CHECKING:
    from .exchange import ImportPlan
    from .session import AuroraSession

logger = logging.getLogger("aurora_gui.pipeline")

# ARMmessage (jsons.txt) — server pushes, `_type:"msg"`, dispatched by mess_id
MESSAGE_NAMES = {
    2: "STUDENT_LOGIN", 3: "LESSON_SWITCH", 4: "TASK_SCORED", 6: "STUDENT_UPDATE_TASK",
    7: "TASK_REFUSED", 8: "TASK_ADDED", 9: "QUIT_REQUESTED", 10: "TRAINER_START",
    11: "CHAT_MESSAGE", 12: "LECTURE_SWITCH", 13: "LECTURE_SYNC", 14: "CHAT_MESSAGE_STREAM",
    16: "COMISSION_CLOSED", 17: "COMISSION_QUEUE_STUDENT_JOIN", 18: "COMISSION_QUEUE_STUDENT_ACCEPTED",
    19: "COMISSION_QUEUE_STUDENT_LEAVE", 20: "COMISSION_TEST_REQUESTED", 21: "COMISSION_TEST_DONE",
    22: "COMISSION_MEMBER_REQUEST", 23: "COMISSION_MEMBER_ADDED", 24: "COMISSION_MEMBER_REMOVED",
    25: "COMISSION_GROUP_ADDED", 26: "COMISSION_GROUP_REMOVED", 27: "COMISSION_STUDENT_END",
    28: "COMISSION_STUDENT_OPEN_TASK", 29: "LESSON_STREAM_STARTED", 30: "LESSON_STREAM_ENDED",
    31: "DBG_OUTPUT", 32: "DBG_STOPPED", 33: "DBG_RUNNING", 34: "DBG_EXITED", 35: "DBG_FRAME_CHANGED",
    36: "COURSE_WORK_SELECTED", 39: "GROUP_SETTINGS_CHANGED", 40: "STUDENT_SETTINGS_CHANGED",
}
LESSON_SWITCH, TASK_SCORED, TASK_REFUSED, TASK_ADDED = 3, 4, 7, 8
QUIT_REQUESTED, TRAINER_START, GROUP_SETTINGS_CHANGED = 9, 10, 39
# pushes after which module.js runs loadTasks()
TASK_LIST_PUSHES = {LESSON_SWITCH, TASK_SCORED, TASK_REFUSED, TASK_ADDED}

NEWS_COUNT = 20            # loadNews({count:20, offset:0})
CHAT_COUNT = 150           # loadChatPrevN({count:150, first_id:0})
NEWS_DEFAULT_LAST_ID = 46  # app.js UpdateNews: userPrefs 'news/lastId' || 46
NEWS_POLL_SECONDS = 60.0   # app.js setInterval(UpdateNews, 60000)

# module.js _taskStatusNames + click rules
STATUS_NAMES = {0: "", 1: "Не выполнено", 2: "На проверке", 3: "Доработать", 4: "Сдано",
                5: "Доработать (оценено)", 6: "На проверке (оценено)", 7: "У куратора", 8: "Сгорела"}
OPENABLE = {1, 3, 4, 5}    # showTask() allowed
EDITABLE = {1, 3, 5}       # 4 opens read-only (_taskReadOnly), no saveTask on close


def status_label(task: dict) -> str:
    st = task.get("status")
    return STATUS_NAMES.get(st, f"Статус {st}") or "—"


def can_open(task: dict) -> bool:
    return task.get("status") in OPENABLE


def can_edit(task: dict) -> bool:
    return task.get("status") in EDITABLE


# ---------------------------------------------------------------------- helpers
class _Tracker:
    """Counts requests fired by a callback chain; wait() returns when the whole chain is done."""

    def __init__(self, sess: "AuroraSession"):
        self.sess = sess
        self.results: Dict[str, dict] = {}
        self._left = 0
        self._cv = threading.Condition()

    def fire(self, ser_task: str, data: Any = None, then: Optional[Callable[[dict], None]] = None,
             key: Optional[str] = None):
        key = key or ser_task
        with self._cv:
            self._left += 1

        def cb(rsp):
            self.results[key] = rsp
            if then:
                try:
                    then(rsp)          # may fire further requests before we count this one done
                except Exception:
                    logger.exception(f"{ser_task} follow-up failed")
            with self._cv:
                self._left -= 1
                self._cv.notify_all()

        self.sess.send(ser_task, data, cb)

    def wait(self, timeout: float = 70.0) -> Dict[str, dict]:
        with self._cv:
            self._cv.wait_for(lambda: self._left == 0, timeout)
        return self.results


def _data(rsp: Optional[dict], default=None):
    if rsp and rsp.get("success"):
        return rsp.get("data")
    return default


def _last_news_id(sess: "AuroraSession") -> int:
    prefs = sess.bootstrap.get("loadUserPreferences")
    if isinstance(prefs, dict) and prefs.get("news/lastId"):
        return prefs["news/lastId"]
    return NEWS_DEFAULT_LAST_ID


# ---------------------------------------------------------------------- post-login
def run_post_login(sess: "AuroraSession", first: bool = True) -> None:
    """fnAuthorize (app.js): loadUserPreferences, then 'arm/authorized' handlers fire in parallel,
    each module chaining its own follow-ups on the replies:

      getStudentNewsSince(<news/lastId||46>)   app.js UpdateNews (+ 60 s timer)
      getComissionCount {}                      (original only; seen on the wire)
      getStats [] , loadNews {20,0}
      getTaskTypes {}        -> getSubjectList {} -> getTaskToolsList {} -> getActiveTest {}
                                                                         -> loadTasks()
      getCurrentCourseInfo {} -> getGroupPreferences {} , loadChatPrevN {150,0}
      checkForLessonStreamStudent {}
      shouldSelectCourseWork {} -> getCourseWorkOptions(<course id>) for each id
      loadTasks() = getTaskList {} -> getUnitsList {} ; getStats [] in parallel

    Blocks until the whole chain has answered; results go to sess.bootstrap, tasks to sess.tasks.
    """
    b = sess.bootstrap
    prefs = sess.call("loadUserPreferences", {})
    if not prefs.get("success"):
        raise ProtocolError(f"loadUserPreferences: {prefs.get('error') or 'success=false'}")
    b["loadUserPreferences"] = prefs.get("data") or {}
    tr = _Tracker(sess)
    on_authorized(sess, tr)
    tr.fire("getComissionCount", {})
    tr.fire("getStats", [])
    tr.fire("loadNews", {"count": NEWS_COUNT, "offset": 0})

    def after_tools(_):
        tr.fire("getActiveTest", {})
        load_tasks(sess, tr)

    tr.fire("getTaskTypes", {}, then=lambda _: tr.fire(
        "getSubjectList", {}, then=lambda _: tr.fire("getTaskToolsList", {}, then=after_tools)))

    def after_course(rsp):
        if not rsp.get("success"):
            return
        tr.fire("getGroupPreferences", {})
        tr.fire("loadChatPrevN", {"count": CHAT_COUNT, "first_id": 0}, then=lambda r: _note_chat(sess, r))

    tr.fire("getCurrentCourseInfo", {}, then=after_course)
    tr.fire("checkForLessonStreamStudent", {})

    def after_courses(rsp):
        courses = _data(rsp, []) or []
        for cid in courses if isinstance(courses, list) else []:
            tr.fire("getCourseWorkOptions", cid, key=f"getCourseWorkOptions:{cid}")

    tr.fire("shouldSelectCourseWork", {}, then=after_courses)
    res = tr.wait()
    for k, rsp in res.items():
        b[k] = _data(rsp)
    logger.info(f"post-login sequence done: {len(sess.tasks)} tasks")


def on_authorized(sess: "AuroraSession", tr: Optional[_Tracker] = None) -> None:
    """Handlers of 'arm/authorized' that also run after every re-authorization:
    UpdateNews() now + a fresh 60 s timer; task module onWSconnected -> loadChatSince."""
    if tr is not None:
        tr.fire("getStudentNewsSince", _last_news_id(sess), then=lambda r: _note_news(sess, r))
    else:
        sess.send("getStudentNewsSince", _last_news_id(sess), lambda r: _note_news(sess, r))
        last_chat = sess.bootstrap.get("_lastChatMessage")
        if last_chat:
            sess.send("loadChatSince", last_chat, lambda r: _note_chat(sess, r))
    start_news_timer(sess)


def start_news_timer(sess: "AuroraSession") -> None:
    """setInterval(UpdateNews, 60000), cleared on 'ws/disconnected' (socket generation changes)."""
    gen = sess.timer_token()

    def tick():
        if not sess.timer_alive(gen):
            return
        if sess.is_authorized:
            sess.send("getStudentNewsSince", _last_news_id(sess), lambda r: _note_news(sess, r))
        sess.schedule(NEWS_POLL_SECONDS, tick)

    sess.schedule(NEWS_POLL_SECONDS, tick)


def _note_news(sess: "AuroraSession", rsp: dict) -> None:
    rows = _data(rsp, []) or []
    if isinstance(rows, list) and rows:
        # The original shows a popup; 'news/lastId' only moves when the user reads it there,
        # so we do not mark anything as read (no saveUserPreferences).
        sess.notify(f"Новостей: {len(rows)} (прочитать можно в официальном клиенте)")


def _note_chat(sess: "AuroraSession", rsp: dict) -> None:
    rows = _data(rsp, []) or []
    times = []
    for m in rows if isinstance(rows, list) else []:
        try:
            times.append(int(m.get("date_added")))
        except (TypeError, ValueError, AttributeError):
            pass
    if times:
        cur = sess.bootstrap.get("_lastChatMessage") or 0
        sess.bootstrap["_lastChatMessage"] = max([cur] + times)


def load_tasks(sess: "AuroraSession", tr: Optional[_Tracker] = None) -> List[dict]:
    """module.js loadTasks: getTaskList -> getUnitsList, getStats [] in parallel."""
    own = tr is None
    tr = tr or _Tracker(sess)

    def got_list(rsp):
        data = _data(rsp)
        if isinstance(data, list):
            sess.tasks = data
            sess.tasks_updated()
        if rsp.get("success"):
            tr.fire("getUnitsList", {})

    tr.fire("getTaskList", {}, then=got_list)
    tr.fire("getStats", [], key="getStats:loadTasks")
    if own:
        res = tr.wait()
        if not (res.get("getTaskList") or {}).get("success"):
            raise ProtocolError(f"getTaskList: {(res.get('getTaskList') or {}).get('error') or 'нет ответа'}")
    return sess.tasks


def install_push_handlers(sess: "AuroraSession") -> None:
    """module.js / app.js reactions to server pushes that touch the network."""
    for mid in TASK_LIST_PUSHES:
        sess.add_push_handler(mid, lambda m: threading.Thread(
            target=_safe, args=(load_tasks, sess), daemon=True).start())
    sess.add_push_handler(LESSON_SWITCH, lambda m: sess.send("getGroupPreferences", {}))
    sess.add_push_handler(GROUP_SETTINGS_CHANGED, lambda m: sess.send("getGroupPreferences", {}))
    sess.add_push_handler(TRAINER_START, lambda m: sess.send("getActiveTest", {}))
    # QUIT_REQUESTED: the quit module closes the socket without reconnecting
    sess.add_push_handler(QUIT_REQUESTED, lambda m: threading.Thread(target=sess.close, daemon=True).start())


def _safe(fn, *a):
    try:
        fn(*a)
    except Exception as e:
        logger.warning(f"{fn.__name__} failed: {e}")


# ---------------------------------------------------------------------- task
def open_task(sess: "AuroraSession", task_id: int) -> Tuple[dict, List[dict]]:
    """showTask: getTask, then initialize only the tools attached to this task.

    Original renderOpenedTask initializes tools in task.tools order. Code (5) loads files;
    tests (6) loads tests and validity. Tasks without a code tool have no file snapshot.
    """
    task = sess.get_task(task_id)
    tr = _Tracker(sess)
    has_code = False
    for tool in task.get("tools") or []:
        tool_id = tool.get("id") if isinstance(tool, dict) else None
        if tool_id == 5:
            has_code = True
            tr.fire("getTaskFiles2", task_id)
        elif tool_id == 6:
            tr.fire("getTaskTests", task_id)
            tr.fire("getTaskTestsValidity", task_id)
    res = tr.wait()
    if not has_code:
        return task, []
    frsp = res.get("getTaskFiles2") or {}
    if not frsp.get("success"):
        raise ProtocolError(f"getTaskFiles2 {task_id}: {frsp.get('error') or 'success=false'}")
    return task, frsp.get("data") or []


def apply_import(sess: "AuroraSession", task_id: int, plan: "ImportPlan", *,
                 duration_seconds: float = 0.0, cancel_event=None, on_progress=None,
                 simulate_keys: bool = False) -> List[str]:
    """Explicit automatic import: timed checks followed by persistence."""
    from . import import_activity
    import_activity.timed_prepare(sess, task_id, plan, duration_seconds, cancel_event, on_progress,
                                  simulate_keys=simulate_keys)
    if cancel_event is not None and cancel_event.is_set():
        raise import_activity.ImportCancelled("Автоматическое применение отменено до сохранения")
    if on_progress:
        on_progress(0.95, "Сохранение на сервере…")
    result = save_import(sess, task_id, plan)
    if on_progress:
        on_progress(1.0, "Сохранено на сервере")
    return result


def save_import(sess: "AuroraSession", task_id: int, plan: "ImportPlan") -> List[str]:
    """Persist an import the way endTask() closes an edited task:
        debugEnd {} (code tool only) -> parallel saveTaskFile -> saveTask -> loadTasks()
    Successful writes return an empty list without reopening the task."""
    if any(isinstance(tool, dict) and tool.get("id") == 5
           for tool in plan.task.get("tools") or []):
        rsp = sess.call("debugEnd", {})
        if not rsp.get("success"):
            raise ProtocolError(f"debugEnd: {rsp.get('error') or 'success=false'}")
    # Original SAZ, ids 113..116: stop debugger, send both files without waiting,
    # then save the document only after every file has acknowledged success.
    tr = _Tracker(sess)
    for index, (name, content) in enumerate(plan.files_to_save):
        tr.fire("saveTaskFile", {"idTask": int(task_id), "name": name, "file": content},
                key=f"saveTaskFile:{index}")
    results = tr.wait()
    for index, (name, _) in enumerate(plan.files_to_save):
        rsp = results.get(f"saveTaskFile:{index}") or {}
        if not rsp.get("success"):
            raise ProtocolError(f"saveTaskFile {name}: {rsp.get('error') or 'success=false'}")
    if plan.changed_fields or plan.files_to_save:
        doc = dict(plan.task)
        for k in ("method_description", "algo_text", "algo_graph"):   # endTask coercion
            if not isinstance(doc.get(k), str):
                doc[k] = ""
        sess.save_task(doc)
    _safe(load_tasks, sess)

    return []

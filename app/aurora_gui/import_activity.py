"""Timed automatic JSON imports with optional, explicitly synthetic keyboard events."""
import copy
import logging
import math
import random
import threading
import time

from .core import AuroraError

logger = logging.getLogger("aurora_gui.import_activity")


class ImportCancelled(AuroraError):
    """Preparation cancelled before any persistent save request."""


def _check_cancel(cancel_event):
    if cancel_event.is_set():
        raise ImportCancelled("Автоматическое применение отменено до сохранения")


def _replacement_versions(old, new, pieces):
    """Keep shared prefix/suffix, replace the changed region in bounded chunks."""
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (suffix < min(len(old), len(new)) - prefix
           and old[len(old) - suffix - 1] == new[len(new) - suffix - 1]):
        suffix += 1
    end = len(new) - suffix if suffix else len(new)
    middle = new[prefix:end]
    tail = new[end:]
    count = max(1, min(pieces, len(middle)))
    for index in range(1, count + 1):
        yield new[:prefix] + middle[:len(middle) * index // count] + tail


def _steps(plan, duration):
    # Document fields are local until the final save, as in the original editor.
    for name in plan.changed_fields:
        yield "field", name, None
    snapshot = dict(plan.original_code_snapshot or [])
    # Intermediate checks are spaced at least roughly one lint debounce apart
    # when the chosen duration permits it; requests remain bounded for long imports.
    budget = max(len(plan.files_to_save), int(duration / 0.6) - len(plan.changed_fields))
    pieces = max(1, min(20, budget // max(1, len(plan.files_to_save))))
    for name, content in plan.files_to_save:
        for version in _replacement_versions(snapshot.get(name, ""), content, pieces):
            snapshot[name] = version
            yield "file", name, [[n, value] for n, value in snapshot.items()]


def timed_prepare(sess, task_id, plan, duration_seconds=0.0, cancel_event=None,
                  on_progress=None, *, simulate_keys=False, clock=None, wait=None,
                  rng=None):
    """Schedule explicit automatic changes; intermediate code checks are read-only.

    Optional keyboard telemetry describes synthetic edits. Server saves happen
    after this function completes. clock/wait permit testing without real delays.
    """
    duration = float(duration_seconds)
    if not math.isfinite(duration) or not 0 <= duration <= 86400:
        raise ValueError("Длительность должна быть от 0 до 1440 минут")
    cancel = cancel_event if cancel_event is not None else threading.Event()
    clock = clock or time.monotonic
    wait = wait or cancel.wait
    rng = rng if rng is not None else random.Random()

    last_fraction = 0.0

    def progress(fraction, text):
        nonlocal last_fraction
        last_fraction = max(last_fraction, fraction)
        if on_progress:
            on_progress(last_fraction, text)

    _check_cancel(cancel)
    progress(0.0, "Автоматическое применение JSON…")
    if not duration:
        prepare(sess, task_id, plan)
        _check_cancel(cancel)
        return
    total = sum(1 for _ in _steps(plan, duration))
    if not total:
        return
    started = clock()
    stage_elapsed = 0.0
    remaining_duration = duration
    working_task = copy.deepcopy(plan.original_task if plan.original_task is not None else plan.task)
    has_code = any(t.get("id") == 5 for t in plan.task.get("tools", []) if isinstance(t, dict))
    from .keyboard_simulation import (KeyboardBuffer, edit_events, text_edits,
                                      keypress_groups, keypress_schedule)
    from .editor_timeline import EditorTimeline, normalized
    keyboard = KeyboardBuffer(sess, clock) if simulate_keys else None
    editor = EditorTimeline(sess, task_id, plan.original_code_snapshot, clock, enabled=has_code)
    try:
        for index, (kind, name, snapshot) in enumerate(_steps(plan, duration), 1):
            stages_left = total - index + 1
            began = started + stage_elapsed
            stage_duration = (remaining_duration if stages_left == 1 else
                              remaining_duration * rng.uniform(.65, 1.35) / stages_left)
            deadline = started + (duration if stages_left == 1 else
                                  stage_elapsed + stage_duration)
            stage_elapsed += stage_duration
            remaining_duration -= stage_duration
            if kind == "file":
                editor.activate(name)

            def wait_until(target):
                while True:
                    _check_cancel(cancel)
                    if keyboard:
                        keyboard.tick()
                    editor.tick()
                    remaining = target - clock()
                    if remaining <= 1e-9:
                        break
                    elapsed = max(0, clock() - started)
                    progress(min(0.9, 0.9 * elapsed / duration),
                             f"Автоматическое применение: {index}/{total}, "
                             f"осталось {max(0, math.ceil(duration - elapsed))} с")
                    next_timer = min(editor.next_due(), keyboard.next_flush if keyboard else math.inf)
                    wait(min(0.2, remaining, max(1e-9, next_timer - clock())))

            edits = []
            if keyboard:
                if kind == "file":
                    edits = [(editor.files[name], dict(snapshot)[name])]
                elif name in ("method2", "algo2", "test-input"):
                    edits = list(text_edits(working_task.get(name), plan.task[name]))

                # Freeze the cursor for both counting and playback; only actual
                # event playback advances the editor state.
                initial_cursor = editor.cursors.get(name) if kind == "file" else None
                def events():
                    for before, after in edits:
                        yield from edit_events(before, after, cursor=initial_cursor)

                count = 0
                for _ in keypress_groups(events()):
                    _check_cancel(cancel)
                    count += 1
                slot_elapsed = 0.0
                remaining_slot = deadline - began
                for number, group in enumerate(keypress_groups(events())):
                    groups_left = count - number
                    slot_start = began + slot_elapsed
                    slot_duration = (remaining_slot if groups_left == 1 else
                                     remaining_slot * rng.uniform(.5, 1.5) / groups_left)
                    slot_end = slot_start + slot_duration
                    slot_elapsed += slot_duration
                    remaining_slot -= slot_duration
                    for when, event in keypress_schedule(group, slot_start, slot_end, rng):
                        wait_until(when)
                        keyboard.emit(event)
                        if kind == "file":
                            editor.apply_event(name, event)
            wait_until(deadline)
            _check_cancel(cancel)
            if kind == "file":
                target_text = normalized(dict(snapshot)[name])
                if keyboard:
                    if editor.files[name] != target_text:
                        raise AuroraError("События клавиатуры не совпали с промежуточным текстом файла")
                else:
                    editor.changed(name, target_text)
            elif kind == "field":
                working_task[name] = copy.deepcopy(plan.task[name])
                working_task["__lastSaved"] = int(time.time())
            progress(0.9 * index / total, f"Применено: {name} ({index}/{total})")
        _check_cancel(cancel)
        # The final change also receives its full 500 ms quiet period.
        while editor.pending:
            wait_until(editor.next_due())
        _check_cancel(cancel)
        plan.task = working_task
    finally:
        if keyboard:
            keyboard.finish()


def prepare(sess, task_id, plan):
    if not plan.files_to_save or not any(t.get("id") == 5 for t in plan.task.get("tools", [])
                                         if isinstance(t, dict)):
        return
    files = plan.code_snapshot if plan.code_snapshot is not None else [list(f) for f in plan.files_to_save]

    def observe(command):
        def callback(rsp):
            if not rsp.get("success"):
                # Editor diagnostics are not save gates in the original.
                logger.warning("%s: сервер отклонил запрос редактора", command)
        return callback

    for name, _ in plan.files_to_save:
        sess.send("getCodeLint", {"idTask": int(task_id), "fileName": name, "files": files},
                  observe("getCodeLint"))

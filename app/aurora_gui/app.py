"""Single-window dark GUI: login -> task list -> export/import JSON (Downloads).

All network work runs in background threads over one AuroraSession; results come back to the
Tk thread through `after()`. Saved credentials are protected by Windows DPAPI.
"""
from __future__ import annotations

import logging
import math
import os
import queue
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import customtkinter as ctk

from . import exchange, pipeline
from . import import_activity
from .core import AuroraError, Config, protocol
from .credentials import CredentialStore
from .settings import UISettings
from .window_chrome import WindowChrome
from .session import (AUTHORIZING, CLOSED, CONNECTED, CONNECTING, DISCONNECTED, READY,
                      RECONNECTING, AuroraSession)

log = logging.getLogger("aurora_gui.app")

ACCENT = "#3b82f6"
BG = "#16181d"
PANEL = "#1f2229"
PANEL_2 = "#272b34"
TEXT = "#e6e8ee"
MUTED = "#9aa3b2"
OK = "#22c55e"
WARN = "#f59e0b"
ERR = "#ef4444"

MIN_UI_SCALE = 80
MAX_UI_SCALE = 180
UI_SCALE_STEP = 10


def normalize_ui_scale(percent: int) -> int:
    """Clamp a requested UI zoom percentage to the supported range."""
    return max(MIN_UI_SCALE, min(MAX_UI_SCALE, int(percent)))

STATE_VIEW = {
    DISCONNECTED: ("Нет соединения", MUTED),
    CONNECTING: ("Подключение…", WARN),
    CONNECTED: ("Соединение установлено", ACCENT),
    AUTHORIZING: ("Авторизация…", WARN),
    READY: ("В сети", OK),
    RECONNECTING: ("Переподключение…", WARN),
    CLOSED: ("Отключено", MUTED),
}


class _QueueLogHandler(logging.Handler):
    def __init__(self, q: "queue.Queue[str]"):
        super().__init__(logging.INFO)
        self.q = q
        self.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            self.q.put_nowait(self.format(record))
        except Exception:
            pass


class AuroraApp(ctk.CTk):
    def __init__(self, config: Optional[Config] = None, *, ui_settings: Optional[UISettings] = None):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self._input_time_origin = (time.monotonic(), time.time() * 1000)
        self.title("Аврора — клиент")
        self.geometry("1040x680")
        self.minsize(820, 540)
        self.configure(fg_color=BG)
        self._settings = ui_settings if ui_settings is not None else UISettings()
        self._ui_scale = self._settings.load_scale()
        self._scale_hide_job = None
        ctk.set_widget_scaling(self._ui_scale / 100)
        self._bind_scale_shortcuts()

        self.cfg = config or Config.from_env()
        self._credentials = CredentialStore(self.cfg.ws_url)
        self.session: Optional[AuroraSession] = None
        self.tasks: List[dict] = []
        self.selected_id: Optional[int] = None
        self._pending_import = None          # (task_id, ImportPlan)
        self._busy = False
        self._import_operation = None
        self._import_token = 0
        self._import_saving = False
        self._logging_out = False
        self.current = ""

        self._log_q: "queue.Queue[str]" = queue.Queue()
        handler = _QueueLogHandler(self._log_q)
        logging.getLogger("aurora_gui").addHandler(handler)
        logging.getLogger("aurora_gui").setLevel(logging.INFO)

        self._style_treeview()
        self._build_topbar()
        self.body = ctk.CTkFrame(self, fg_color=BG, corner_radius=0)
        self.body.pack(fill="both", expand=True)
        self._build_log()
        self.frames: Dict[str, ctk.CTkFrame] = {
            "login": self._build_login(),
            "tasks": self._build_tasks(),
            "review": self._build_review(),
        }
        self.show("login")
        self.protocol("WM_DELETE_WINDOW", self._on_quit)
        self.after_idle(self._install_window_chrome)
        self.after(100, self._pump_log)
        self.after(50, self._new_session)

    # ================================================================== plumbing
    def run_bg(self, fn: Callable[[], Any], on_ok: Callable[[Any], None] = None,
               on_err: Callable[[Exception], None] = None, busy: str = None):
        if busy:
            self._set_busy(busy)

        def work():
            try:
                res = fn()
            except Exception as e:  # shown to the user; never contains secrets
                log.debug("background task failed", exc_info=True)
                self.after(0, lambda err=e: (self._set_busy(None), (on_err or self._show_error)(err)))
            else:
                self.after(0, lambda: (self._set_busy(None), on_ok and on_ok(res)))

        threading.Thread(target=work, daemon=True).start()

    def _set_busy(self, text: Optional[str]):
        self._busy = bool(text)
        if text:
            self.progress.grid()
            self.progress.start()
            self.flash(text, MUTED)
        else:
            self.progress.stop()
            self.progress.grid_remove()

    def flash(self, text: str, color: str = MUTED):
        self.msg_label.configure(text=text, text_color=color)

    def _show_error(self, e: Exception):
        self.flash(f"Ошибка: {e}", ERR)

    def show(self, name: str):
        for n, f in self.frames.items():
            if n == name:
                f.pack(fill="both", expand=True, padx=16, pady=(12, 8))
            else:
                f.pack_forget()
        self.current = name

    def _pump_log(self):
        try:
            while True:
                line = self._log_q.get_nowait()
                self.logbox.configure(state="normal")
                self.logbox.insert("end", line + "\n")
                self.logbox.see("end")
                self.logbox.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(150, self._pump_log)

    # ================================================================== session
    def _new_session(self):
        self.session = AuroraSession(
            self.cfg,
            input_time_origin=self._input_time_origin,
            on_state=lambda st: self.after(0, self._on_state, st),
            on_push=lambda mid, msg: self.after(0, self._on_push, mid, msg),
            on_error=lambda text: self.after(0, self.flash, text, WARN),
            on_notice=lambda text: self.after(0, self.flash, text, ACCENT),
            on_tasks=lambda tasks: self.after(0, self._render_tasks, tasks),
        )
        # like the original page load: open the socket and ask the version before the login form
        self.run_bg(self.session.connect, on_err=lambda e: self.flash(
            f"Сервер недоступен: {e}. Попробуйте войти ещё раз.", ERR))

    def _on_state(self, st: str):
        text, color = STATE_VIEW.get(st, (st, MUTED))
        self.state_dot.configure(text_color=color)
        self.state_label.configure(text=text)
        if st == CLOSED and self.current != "login" and not self._logging_out:
            # QUIT_REQUESTED from the server: the original closes the socket for good
            self._logout(reason="Сервер завершил сеанс", forget=False)

    def _on_push(self, mess_id: int, msg: dict):
        # the session already reacts on the network side (loadTasks etc., see pipeline);
        # the list is re-rendered through on_tasks
        name = pipeline.MESSAGE_NAMES.get(mess_id, f"mess_id {mess_id}")
        texts = {pipeline.TASK_SCORED: "Работа оценена", pipeline.TASK_REFUSED: "Работа возвращена на доработку",
                 pipeline.TASK_ADDED: "Выдана новая работа",
                 pipeline.QUIT_REQUESTED: "Сервер запросил завершение сеанса"}
        self.flash(texts.get(mess_id, f"Сообщение сервера: {name}"), ACCENT)

    def _on_quit(self):
        operation = getattr(self, "_import_operation", None)
        if operation:
            operation["cancel_event"].set()
            self._import_operation = None
            self._import_saving = False
            self._set_busy(None)
        try:
            if self.session:
                self.session.close()
        finally:
            self.destroy()

    # ================================================================== top bar / log
    def _build_topbar(self):
        bar = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0, height=48)
        bar.pack(fill="x")
        self.topbar = bar
        self.brand_label = ctk.CTkLabel(bar, text="АСО «Аврора»", font=ctk.CTkFont(size=16, weight="bold"),
                                      text_color=TEXT)
        self.brand_label.pack(side="left", padx=(16, 12), pady=10)
        self.state_dot = ctk.CTkLabel(bar, text="●", text_color=MUTED, font=ctk.CTkFont(size=14))
        self.state_dot.pack(side="left")
        self.state_label = ctk.CTkLabel(bar, text="Нет соединения", text_color=MUTED)
        self.state_label.pack(side="left", padx=(4, 0))
        self.scale_label = ctk.CTkLabel(bar, text="", text_color=MUTED)
        # Shown only after a zoom shortcut, directly after the connection state.
        self.window_buttons = ctk.CTkFrame(bar, fg_color="transparent")
        ctk.CTkButton(self.window_buttons, text="—", width=38, height=32,
                      fg_color="transparent", hover_color=PANEL_2,
                      command=lambda: self._window_chrome.minimize()).pack(side="left")
        self.maximize_btn = ctk.CTkButton(self.window_buttons, text="□", width=38, height=32,
                      fg_color="transparent", hover_color=PANEL_2, command=self._toggle_maximize)
        self.maximize_btn.pack(side="left")
        ctk.CTkButton(self.window_buttons, text="✕", width=38, height=32,
                      fg_color="transparent", hover_color="#b52c35",
                      command=self._on_quit).pack(side="left")
        self.logout_btn = ctk.CTkButton(bar, text="Выйти", width=80, fg_color=PANEL_2,
                                        hover_color="#353a45", command=self._logout)
        self.user_label = ctk.CTkLabel(bar, text="", text_color=TEXT)
        self.user_label.pack(side="right", padx=16)

    def _install_window_chrome(self):
        self._window_chrome = WindowChrome(self)
        try:
            installed = self._window_chrome.install()
        except OSError:
            log.warning("Не удалось объединить панель окна; используется стандартная рамка")
            return
        if not installed:
            return
        # Reserve window controls before flexible labels, including at high zoom.
        self.window_buttons.pack(side="right", before=self.brand_label, padx=(4, 6))
        for widget in (self.topbar, self.brand_label, self.state_dot, self.state_label,
                       self.scale_label, self.user_label):
            widget.bind("<ButtonPress-1>", self._window_chrome.start_move, add="+")
            widget.bind("<Double-Button-1>", self._toggle_maximize, add="+")
        self.bind("<Configure>", self._update_maximize_button, add="+")

    def _toggle_maximize(self, _event=None):
        self._window_chrome.toggle_maximize()
        self._update_maximize_button()
        return "break"

    def _update_maximize_button(self, _event=None):
        if _event is not None and _event.widget is not self:
            return
        self.maximize_btn.configure(text="❐" if self._window_chrome.is_maximized() else "□")

    def _build_log(self):
        bottom = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=0)
        bottom.pack(fill="x", side="bottom")
        bottom.grid_columnconfigure(0, weight=1)
        self.msg_label = ctk.CTkLabel(bottom, text="", text_color=MUTED, anchor="w")
        self.msg_label.grid(row=0, column=0, sticky="ew", padx=16, pady=(6, 2))
        self.progress = ctk.CTkProgressBar(bottom, mode="indeterminate", width=160, progress_color=ACCENT)
        self.progress.grid(row=0, column=1, padx=16)
        self.progress.grid_remove()
        self.log_toggle = ctk.CTkButton(bottom, text="Журнал ▴", width=90, fg_color="transparent",
                                        hover_color=PANEL_2, text_color=MUTED, command=self._toggle_log)
        self.log_toggle.grid(row=0, column=2, padx=(0, 8))
        self.logbox = ctk.CTkTextbox(bottom, height=130, fg_color=BG, text_color=MUTED,
                                     font=ctk.CTkFont(family="Consolas", size=11))
        self.logbox.configure(state="disabled")
        self._log_visible = False

    def _toggle_log(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self.logbox.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12, pady=(0, 8))
            self.log_toggle.configure(text="Журнал ▾")
        else:
            self.logbox.grid_remove()
            self.log_toggle.configure(text="Журнал ▴")

    # ================================================================== login
    def _build_login(self) -> ctk.CTkFrame:
        outer = ctk.CTkFrame(self.body, fg_color=BG)
        card = ctk.CTkFrame(outer, fg_color=PANEL, corner_radius=14)
        card.place(relx=0.5, rely=0.45, anchor="center")
        ctk.CTkLabel(card, text="Вход", font=ctk.CTkFont(size=22, weight="bold"),
                     text_color=TEXT).pack(padx=40, pady=(28, 4), anchor="w")
        ctk.CTkLabel(card, text="Логин (почта) и пароль от «Авроры»", text_color=MUTED).pack(
            padx=40, anchor="w")
        self.login_entry = ctk.CTkEntry(card, width=320, height=38, placeholder_text="Логин")
        self.login_entry.pack(padx=40, pady=(18, 8))
        if self.cfg.login:
            self.login_entry.insert(0, self.cfg.login)
        self.pass_entry = ctk.CTkEntry(card, width=320, height=38, placeholder_text="Пароль", show="•")
        self.pass_entry.pack(padx=40, pady=(0, 6))
        self._password_visible = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(card, text="Показать пароль", variable=self._password_visible,
                        command=self._toggle_password).pack(padx=40, pady=(4, 4), anchor="w")
        self.login_btn = ctk.CTkButton(card, text="Войти", width=320, height=40,
                                       fg_color=ACCENT, command=self._do_login)
        self.login_btn.pack(padx=40, pady=(14, 8))
        self.login_status = ctk.CTkLabel(card, text="", text_color=MUTED, wraplength=320)
        self.login_status.pack(padx=40, pady=(0, 24))
        try:
            saved = self._credentials.load()
            if saved:
                self.login_entry.delete(0, "end")
                self.login_entry.insert(0, saved[0])
                self.pass_entry.insert(0, saved[1])
        except (OSError, ValueError):
            self.login_status.configure(text="Не удалось прочитать сохранённый пароль", text_color=WARN)
        self.pass_entry.bind("<Return>", lambda e: self._do_login())
        self.login_entry.bind("<Return>", lambda e: self.pass_entry.focus_set())
        return outer

    def _toggle_password(self):
        self.pass_entry.configure(show="" if self._password_visible.get() else "•")

    def _do_login(self):
        if self._busy:
            return
        login = self.login_entry.get().strip()
        password = self.pass_entry.get()
        if not login or not password:
            self.login_status.configure(text="Введите логин и пароль", text_color=WARN)
            return
        self.login_btn.configure(state="disabled")
        self.login_status.configure(text="Вход…", text_color=MUTED)
        sess = self.session

        def work():
            if sess.state == CLOSED:
                raise AuroraError("Сессия закрыта")
            return sess.login(login, password)

        def ok(rsp):
            save_failed = False
            try:
                self._credentials.save(login, password)
            except OSError:
                save_failed = True
            self._password_visible.set(False)
            self._toggle_password()
            self.login_btn.configure(state="normal")
            self.login_status.configure(text="")
            st = rsp.get("student") or {}
            name = " ".join(x for x in (st.get("student_suname"), st.get("student_name")) if x)
            self.user_label.configure(text=f"{name} · {st.get('group_name', '')}")
            self.logout_btn.pack(side="right", before=self.brand_label, padx=(0, 4))
            if rsp.get("must_change_passw"):
                self.flash("Сервер требует сменить пароль (в официальном клиенте)", WARN)
            elif save_failed:
                self.flash("Вход выполнен, но сохранить пароль не удалось", WARN)
            else:
                self.flash("Вход выполнен", OK)
            self.show("tasks")
            self._render_tasks(self.session.tasks)
            if not self.session.tasks:
                self._refresh_tasks()

        def err(e):
            self.login_btn.configure(state="normal")
            self.login_status.configure(text=str(e), text_color=ERR)

        self.run_bg(work, ok, err, busy="Авторизация…")

    def _logout(self, reason: str = "Сеанс завершён", forget: bool = True):
        operation = getattr(self, "_import_operation", None)
        if operation:
            operation["cancel_event"].set()
            self._import_operation = None
            self._import_saving = False
            self._set_busy(None)
        if forget:
            try:
                self._credentials.delete()
            except OSError:
                self.flash("Не удалось удалить сохранённый пароль. Повторите выход.", ERR)
                return
            self.pass_entry.delete(0, "end")
            self._password_visible.set(False)
            self._toggle_password()
        self._logging_out = True
        try:
            if self.session:
                self.session.close()
        finally:
            self._logging_out = False
        self.tasks, self.selected_id, self._pending_import = [], None, None
        self.user_label.configure(text="")
        self.logout_btn.pack_forget()
        self.show("login")
        self.flash(reason, MUTED)
        self._new_session()

    # ================================================================== tasks
    def _style_treeview(self):
        st = ttk.Style(self)
        st.theme_use("clam")
        self._tree_style = st
        st.configure("Aurora.Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                     borderwidth=0)
        st.configure("Aurora.Treeview.Heading", background=PANEL_2, foreground=MUTED, relief="flat")
        st.map("Aurora.Treeview", background=[("selected", "#2d4a7a")], foreground=[("selected", TEXT)])
        st.map("Aurora.Treeview.Heading", background=[("active", PANEL_2)])
        # drop the clam field border (the light frame around the table)
        st.layout("Aurora.Treeview", [("Aurora.Treeview.treearea", {"sticky": "nswe"})])
        self._apply_treeview_scale()

    def _apply_treeview_scale(self):
        """Scale ttk styles and fixed column widths alongside CustomTkinter."""
        factor = getattr(self, "_ui_scale", 100) / 100
        style = getattr(self, "_tree_style", None)
        if style is not None:
            style.configure("Aurora.Treeview", rowheight=round(36 * factor),
                            font=("Segoe UI", max(1, round(13 * factor))))
            style.configure("Aurora.Treeview.Heading", font=("Segoe UI", max(1, round(12 * factor)), "bold"))
        tree = getattr(self, "tree", None)
        if tree is not None:
            for column, width in (("id", 80), ("name", 300), ("status", 150), ("score", 64)):
                tree.column(column, width=round(width * factor))

    def _set_ui_scale(self, percent: int):
        percent = normalize_ui_scale(percent)
        if percent == self._ui_scale:
            return
        self._ui_scale = percent
        ctk.set_widget_scaling(self._ui_scale / 100)
        self._apply_treeview_scale()
        self.scale_label.configure(text=f"{self._ui_scale}%", text_color=MUTED)
        self.scale_label.pack(side="left", after=self.state_label, padx=(10, 0))
        if self._scale_hide_job is not None:
            self.after_cancel(self._scale_hide_job)
        self._scale_hide_job = self.after(2000, self._fade_scale_notice)
        try:
            self._settings.save_scale(self._ui_scale)
        except OSError:
            self.flash("Масштаб изменён, но сохранить настройку не удалось", WARN)

    def _hide_scale_notice(self):
        self.scale_label.pack_forget()
        self._scale_hide_job = None

    def _fade_scale_notice(self, started=None):
        now = time.monotonic()
        started = now if started is None else started
        fraction = max(0.0, min(1.0, (now - started) / 0.5))
        if fraction >= 1.0:
            self._hide_scale_notice()
            return
        # Smoothly blend the text into its panel; window opacity stays unchanged.
        amount = fraction * fraction * (3 - 2 * fraction)
        channels = [round(int(MUTED[i:i+2], 16) * (1 - amount) +
                          int(PANEL[i:i+2], 16) * amount) for i in (1, 3, 5)]
        self.scale_label.configure(text_color="#" + "".join(f"{c:02x}" for c in channels))
        self._scale_hide_job = self.after(16, self._fade_scale_notice, started)

    def _zoom_in(self):
        self._set_ui_scale(self._ui_scale + UI_SCALE_STEP)

    def _zoom_out(self):
        self._set_ui_scale(self._ui_scale - UI_SCALE_STEP)

    def _bind_scale_shortcuts(self):
        for key in ("plus", "equal", "KP_Add"):
            self.bind_all(f"<Control-KeyPress-{key}>", self._shortcut_zoom_in)
        for key in ("minus", "KP_Subtract"):
            self.bind_all(f"<Control-KeyPress-{key}>", self._shortcut_zoom_out)
        for key in ("0", "KP_0"):
            self.bind_all(f"<Control-KeyPress-{key}>", self._shortcut_zoom_reset)

    def _shortcut_zoom_in(self, _event=None):
        self._zoom_in()
        return "break"

    def _shortcut_zoom_out(self, _event=None):
        self._zoom_out()
        return "break"

    def _shortcut_zoom_reset(self, _event=None):
        self._set_ui_scale(100)
        return "break"

    def _build_tasks(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self.body, fg_color=BG)
        f.grid_columnconfigure(0, weight=3, uniform="task-panels", minsize=320)
        f.grid_columnconfigure(1, weight=2, uniform="task-panels", minsize=270)
        f.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(f, fg_color=BG)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(head, text="Работы", font=ctk.CTkFont(size=18, weight="bold"),
                     text_color=TEXT).pack(side="left")
        self.refresh_btn = ctk.CTkButton(head, text="Обновить", width=100, fg_color=PANEL_2,
                                         hover_color="#353a45", command=self._refresh_tasks)
        self.refresh_btn.pack(side="right")
        self.search = ctk.CTkEntry(head, width=220, placeholder_text="Поиск по названию")
        self.search.pack(side="right", padx=8)
        self.search.bind("<KeyRelease>", lambda e: self._render_tasks(self.tasks))

        box = ctk.CTkFrame(f, fg_color=PANEL, corner_radius=10)
        box.grid(row=1, column=0, sticky="nsew")
        box.grid_rowconfigure(0, weight=1)
        box.grid_columnconfigure(0, weight=1)
        cols = ("id", "name", "status", "score")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", style="Aurora.Treeview",
                                 selectmode="browse")
        for c, title, w, anchor in (("id", "ID", 80, "w"), ("name", "Название", 300, "w"),
                                    ("status", "Статус", 150, "w"), ("score", "Балл", 56, "center")):
            self.tree.heading(c, text=title, anchor=anchor)
            self.tree.column(c, width=w, anchor=anchor, stretch=(c == "name"))
        self.tree.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        sb = ctk.CTkScrollbar(box, orientation="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns", pady=8, padx=(0, 4))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.tag_configure("locked", foreground=MUTED)

        side = ctk.CTkFrame(f, fg_color=PANEL, corner_radius=10)
        side.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(12, 0))
        side.pack_propagate(False)
        self.detail_title = ctk.CTkLabel(side, text="Выберите работу", wraplength=280, justify="left",
                                         font=ctk.CTkFont(size=15, weight="bold"), text_color=TEXT)
        self.detail_title.pack(anchor="w", padx=18, pady=(18, 4))
        self.detail_meta = ctk.CTkLabel(side, text="", text_color=MUTED, justify="left", wraplength=280)
        self.detail_meta.pack(anchor="w", padx=18)

        ctk.CTkLabel(side, text="Выгрузка", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=18, pady=(22, 4))
        self.fmt = ctk.CTkSegmentedButton(side, values=["Обмен (json-decisions)", "Полная копия"])
        self.fmt.set("Обмен (json-decisions)")
        self.fmt.pack(anchor="w", padx=18, fill="x")
        self.export_btn = ctk.CTkButton(side, text="Выгрузить в JSON → Загрузки", height=38,
                                        fg_color=ACCENT, command=self._export, state="disabled")
        self.export_btn.pack(anchor="w", padx=18, pady=(10, 0), fill="x")

        ctk.CTkLabel(side, text="Загрузка", text_color=MUTED,
                     font=ctk.CTkFont(size=12, weight="bold")).pack(anchor="w", padx=18, pady=(22, 4))
        self.import_btn = ctk.CTkButton(side, text="Загрузить из JSON…", height=38, fg_color=PANEL_2,
                                        hover_color="#353a45", command=self._import, state="disabled")
        self.import_btn.pack(anchor="w", padx=18, fill="x")
        self.detail_hint = ctk.CTkLabel(side, text="Перед отправкой на сервер покажу, что изменится.",
                                        text_color=MUTED, wraplength=280, justify="left", anchor="w")
        self.detail_hint.pack(anchor="w", padx=18, pady=(6, 0))
        ctk.CTkButton(side, text="Открыть папку «Загрузки»", fg_color="transparent", hover_color=PANEL_2,
                      text_color=MUTED, command=self._open_downloads).pack(anchor="w", padx=12, pady=(18, 12))
        side.bind("<Configure>", self._resize_task_detail)
        return f

    def _resize_task_detail(self, event):
        """Wrap variable detail text to the allocated side-panel width."""
        factor = getattr(self, "_ui_scale", 100) / 100
        wrap = max(120, round(event.width / factor - 40))
        for name in ("detail_title", "detail_meta", "detail_hint"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.configure(wraplength=wrap)

    def _refresh_tasks(self, quiet: bool = False):
        if not self.session or not self.session.is_authorized:
            return
        self.run_bg(self.session.list_tasks, self._render_tasks,
                    busy=None if quiet else "Загрузка списка работ…")

    def _render_tasks(self, tasks: List[dict]):
        self.tasks = list(tasks or [])
        q = self.search.get().strip().lower() if hasattr(self, "search") else ""
        self.tree.delete(*self.tree.get_children())
        for t in self.tasks:
            name = str(t.get("name") or "")
            if q and q not in name.lower():
                continue
            score = t.get("score")
            self.tree.insert("", "end", iid=str(t.get("id")), tags=() if pipeline.can_open(t) else ("locked",),
                             values=(t.get("id"), name, pipeline.status_label(t),
                                     "–" if score in (None, "") else score))
        if self.selected_id is not None and self.tree.exists(str(self.selected_id)):
            self.tree.selection_set(str(self.selected_id))
        if not quiet_text_set(self):
            self.flash(f"Работ: {len(self.tasks)}", MUTED)

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected_id = int(sel[0])
        t = next((x for x in self.tasks if x.get("id") == self.selected_id), {})
        self.detail_title.configure(text=t.get("name") or f"Работа {self.selected_id}")
        meta = [f"ID {self.selected_id}", f"Статус: {pipeline.status_label(t)}"]
        if t.get("score") not in (None, ""):
            meta.append(f"Балл: {t.get('score')}")
        if not pipeline.can_open(t):
            meta.append("Открыть нельзя (как и в официальном клиенте)")
        elif not pipeline.can_edit(t):
            meta.append("Только просмотр — загрузка недоступна")
        self.detail_meta.configure(text="\n".join(meta))
        self.export_btn.configure(state="normal" if pipeline.can_open(t) else "disabled")
        self.import_btn.configure(state="normal" if pipeline.can_edit(t) else "disabled")

    def _task_name(self, tid: int) -> str:
        t = next((x for x in self.tasks if x.get("id") == tid), {})
        return t.get("name") or f"task_{tid}"

    # ================================================================== export
    def _export(self):
        tid = self.selected_id
        if tid is None:
            return
        full = self.fmt.get() == "Полная копия"
        sess = self.session

        def work():
            task, files = sess.open_task(tid)
            if full:
                doc = exchange.build_backup(tid, task, files, sess.cfg.protocol_version)
                name = f"task_{tid}.json"
            else:
                doc = exchange.build_exchange(task, files)
                name = exchange.sanitize_file_name(task.get("name") or self._task_name(tid)) + ".json"
            return exchange.write_json(exchange.downloads_dir(), name, doc)

        self.run_bg(work, lambda p: self.flash(f"Сохранено: {p}", OK), busy="Выгрузка…")

    def _open_downloads(self):
        d = exchange.downloads_dir()
        try:
            os.startfile(str(d))  # type: ignore[attr-defined]
        except AttributeError:
            subprocess.Popen(["xdg-open", str(d)])

    # ================================================================== import
    def _import(self):
        tid = self.selected_id
        if tid is None:
            return
        path = filedialog.askopenfilename(
            parent=self, title="JSON для загрузки", initialdir=str(exchange.downloads_dir()),
            filetypes=[("JSON", "*.json")])
        if not path:
            return
        sess = self.session

        def work():
            data = exchange.read_import(Path(path))
            if data.task_id is not None and int(data.task_id) != tid:
                raise AuroraError(f"Файл относится к работе {data.task_id}, а выбрана {tid}")
            task, files = sess.open_task(tid)
            return exchange.plan_import(task, files, data), data

        self.run_bg(work, lambda r: self._show_review(tid, Path(path), *r), busy="Чтение файла…")

    def _build_review(self) -> ctk.CTkFrame:
        f = ctk.CTkFrame(self.body, fg_color=BG)
        self.review_title = ctk.CTkLabel(f, text="", font=ctk.CTkFont(size=18, weight="bold"),
                                         text_color=TEXT, wraplength=900, justify="left")
        self.review_title.pack(anchor="w", pady=(0, 8))
        timing = ctk.CTkFrame(f, fg_color="transparent")
        timing.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(timing, text="Длительность автоматического применения, мин:",
                     text_color=TEXT).pack(side="left")
        self.import_duration_entry = ctk.CTkEntry(timing, width=90, placeholder_text="0")
        self.import_duration_entry.insert(0, "0")
        self.import_duration_entry.pack(side="left", padx=10)
        ctk.CTkLabel(f, text="0 — сразу. Отправка и ответы сервера займут дополнительное время.",
                     text_color=MUTED, anchor="w").pack(fill="x", pady=(0, 8))
        self.simulate_keys_var = tk.BooleanVar(master=self, value=True)
        self.simulate_keys_checkbox = ctk.CTkCheckBox(
            f, text="Синтетические нажатия (sendKBE)", variable=self.simulate_keys_var,
            text_color=TEXT)
        self.simulate_keys_checkbox.pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            f, text="Работает при длительности больше 0; при 0 — быстрый импорт без нажатий. "
                    "События формируются автоматически из изменений текста.",
            text_color=MUTED, anchor="w", wraplength=900, justify="left").pack(
                fill="x", pady=(0, 8))
        self.review_text = ctk.CTkTextbox(f, fg_color=PANEL, text_color=TEXT, corner_radius=10,
                                          font=ctk.CTkFont(family="Consolas", size=12))
        self.review_text.pack(fill="both", expand=True)
        self.review_progress_label = ctk.CTkLabel(f, text="", text_color=MUTED, anchor="w")
        self.review_progress_label.pack(fill="x", pady=(8, 2))
        self.review_progress = ctk.CTkProgressBar(f, mode="determinate", progress_color=ACCENT)
        self.review_progress.set(0)
        self.review_progress.pack(fill="x")
        self.review_progress.pack_forget()
        row = ctk.CTkFrame(f, fg_color=BG)
        row.pack(fill="x", pady=(10, 0))
        self.send_btn = ctk.CTkButton(row, text="Отправить на сервер", height=40, fg_color=ACCENT,
                                      command=self._send_import)
        self.send_btn.pack(side="right")
        self.cancel_import_btn = ctk.CTkButton(row, text="Отмена", height=40, fg_color=PANEL_2,
                                                hover_color="#353a45", command=self._cancel_import)
        self.cancel_import_btn.pack(side="right", padx=8)
        return f

    def _show_review(self, tid: int, path: Path, plan: "exchange.ImportPlan", data: "exchange.ImportData"):
        self._pending_import = (tid, plan)
        self.review_title.configure(text=f"Загрузка в «{self._task_name(tid)}» из {path.name}")
        lines = [f"Формат файла: {'обмен (json-decisions)' if data.kind == 'exchange' else 'полная копия'}", ""]
        lines.append("Поля задания (saveTask):")
        if plan.changed_fields:
            lines += [f"  • {k} — будет заменено" for k in plan.changed_fields]
        else:
            lines.append("  — без изменений")
        lines += ["", "Файлы кода (saveTaskFile):"]
        if data.files is None:
            lines.append("  — в JSON нет кода, файлы на сервере не трогаем")
        else:
            lines += [f"  • {n} — будет записан ({len(c)} симв.)" for n, c in plan.files_to_save]
            if plan.files_to_save:
                lines += ["Импорт кода: проверка синтаксиса и сохранение изменённых файлов."]
            lines += [f"  • {n} — совпадает, пропуск" for n in plan.files_unchanged]
            lines += [f"  • {n} — есть только на сервере, остаётся" for n in plan.files_kept]
        if data.warnings:
            lines += ["", "Предупреждения:"] + [f"  ! {w}" for w in data.warnings]
        nothing = not plan.changed_fields and not plan.files_to_save
        lines += ["", "Изменений нет — отправлять нечего." if nothing else
                  "Сохранение завершится после подтверждения файлов и документа сервером."]
        self.review_text.configure(state="normal")
        self.review_text.delete("1.0", "end")
        self.review_text.insert("1.0", "\n".join(lines))
        self.review_text.configure(state="disabled")
        self.send_btn.configure(state="disabled" if nothing else "normal")
        self.show("review")
        self.flash("Проверьте изменения перед отправкой", MUTED)

    def _cancel_import(self):
        operation = getattr(self, "_import_operation", None)
        if operation:
            if self._import_saving:
                self.flash("Сохранение на сервере…", MUTED)
                return
            operation["user_cancel"] = True
            operation["cancel_event"].set()
            self.cancel_import_btn.configure(state="disabled")
            self.flash("Останавливаю импорт…", MUTED)
            return
        self._pending_import = None
        self.show("tasks")
        self.flash("Загрузка отменена", MUTED)

    @staticmethod
    def _parse_import_duration(value: str) -> float:
        """Parse a review duration in minutes and return seconds."""
        raw = (value or "").strip()
        if not raw:
            raw = "0"
        if "," in raw:
            if "." in raw or raw.count(",") != 1:
                raise ValueError("Введите длительность числом (например, 1,5)")
            raw = raw.replace(",", ".")
        try:
            minutes = float(raw)
        except (TypeError, ValueError):
            raise ValueError("Введите длительность числом (например, 1,5)")
        if not math.isfinite(minutes) or minutes < 0 or minutes > 1440:
            raise ValueError("Длительность должна быть от 0 до 1440 минут")
        return minutes * 60.0

    def _set_import_progress(self, token: int, fraction: float, text: str):
        operation = getattr(self, "_import_operation", None)
        if not operation or operation["token"] != token:
            return
        value = min(1.0, max(0.0, float(fraction)))
        self.review_progress.set(value)
        self.review_progress_label.configure(text=f"{text}  {round(value * 100)}%")
        if text == "Сохранение на сервере…":
            self._import_saving = True
            self.cancel_import_btn.configure(state="disabled")

    def _send_import(self):
        if getattr(self, "_busy", False) or getattr(self, "_import_operation", None):
            return
        if not self._pending_import:
            return
        try:
            duration_seconds = self._parse_import_duration(self.import_duration_entry.get())
        except (ValueError, AttributeError) as e:
            self.flash(str(e), WARN)
            return
        simulate_keys = bool(self.simulate_keys_var.get()) and duration_seconds > 0
        tid, plan = self._pending_import
        self._import_token = getattr(self, "_import_token", 0) + 1
        token = self._import_token
        operation = {"token": token, "cancel_event": threading.Event(), "user_cancel": False}
        self._import_operation = operation
        self._import_saving = False
        self.send_btn.configure(state="disabled")
        self.import_duration_entry.configure(state="disabled")
        self.simulate_keys_checkbox.configure(state="disabled")
        self.cancel_import_btn.configure(state="normal")
        self.review_progress_label.configure(text="Подготовка…  0%")
        self.review_progress.set(0)
        self.review_progress.pack(fill="x")
        self.logout_btn.configure(state="disabled")
        sess = self.session

        def work():
            return pipeline.apply_import(
                sess, tid, plan, duration_seconds=duration_seconds,
                simulate_keys=simulate_keys,
                cancel_event=operation["cancel_event"],
                on_progress=lambda percent, text: self._import_progress_from_worker(token, percent, text))

        def finish_controls():
            current = getattr(self, "_import_operation", None)
            if not current or current["token"] != token:
                return False
            self._import_operation = None
            self._import_saving = False
            self._set_busy(None)
            self.logout_btn.configure(state="normal")
            self.import_duration_entry.configure(state="normal")
            self.simulate_keys_checkbox.configure(state="normal")
            self.cancel_import_btn.configure(state="normal")
            self.review_progress.pack_forget()
            self.review_progress_label.configure(text="")
            return True

        def ok(_result):
            if not finish_controls():
                return
            self._pending_import = None
            self.show("tasks")
            self.flash("Сервер подтвердил сохранение", OK)
            # the list was already re-read by loadTasks inside save_import (on_tasks re-renders it)

        def err(e):
            if not finish_controls():
                return
            if isinstance(e, import_activity.ImportCancelled):
                self._pending_import = None
                self.show("tasks")
                self.flash("Загрузка остановлена", MUTED)
                return
            self.send_btn.configure(state="normal")
            self._show_error(e)

        self._set_busy("Автоматическое применение JSON…")

        def worker():
            try:
                result = work()
            except Exception as e:
                log.debug("background task failed", exc_info=True)
                try:
                    self.after(0, lambda error=e: err(error))
                except (RuntimeError, tk.TclError):
                    pass  # Window already closed; cancellation was signalled by _on_quit.
            else:
                try:
                    self.after(0, lambda: ok(result))
                except (RuntimeError, tk.TclError):
                    pass

        threading.Thread(target=worker, daemon=True).start()

    def _import_progress_from_worker(self, token: int, percent: float, text: str):
        operation = getattr(self, "_import_operation", None)
        if not operation or operation["token"] != token:
            return
        # Mark the persistence boundary synchronously so a click cannot race the UI callback.
        if text == "Сохранение на сервере…":
            self._import_saving = True
        try:
            self.after(0, lambda: self._set_import_progress(token, percent, text))
        except (RuntimeError, tk.TclError):
            operation["cancel_event"].set()


def quiet_text_set(app: AuroraApp) -> bool:
    """Keep an important status message (error/success) instead of overwriting it with a count."""
    return app.msg_label.cget("text_color") in (ERR, OK, WARN) and bool(app.msg_label.cget("text"))


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = AuroraApp()
    app.mainloop()


if __name__ == "__main__":
    main()

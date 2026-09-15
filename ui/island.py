"""Quiet Instrument island and Jarvis 1.0 Beta Command Center."""
import json
import os
import platform
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

from core import actions, ai, memory, settings as cfg, skills
from ui import theme


CHROMA = "#010203"
P = theme.palette("light" if cfg.UI_THEME == "light" else "dark")

ACCENTS = {
    "cyan": P.accent_primary,
    "white": P.text_primary,
    "blue": P.info,
    "green": P.success,
    "amber": P.warning,
}

STATUS_LABELS = {
    "idle": "НА СВЯЗИ",
    "paused": "ПАУЗА",
    "starting": "ЗАПУСК",
    "checking": "ПРОВЕРКА",
    "listening": "СЛУШАЮ",
    "hearing": "СЛЫШУ",
    "thinking": "ДУМАЮ",
    "acting": "ВЫПОЛНЯЮ",
    "speaking": "ОТВЕЧАЮ",
    "asking": "НУЖНО ПОДТВЕРЖДЕНИЕ",
    "dictating": "ДИКТОВКА",
    "reminder": "НАПОМИНАНИЕ",
    "error": "ОШИБКА",
}

PILL_STATUS_LABELS = {"asking": "ПОДТВЕРЖДЕНИЕ"}


class UiState:
    def __init__(self):
        self._lock = threading.RLock()
        self.status = "idle"
        self.level = 0.0
        self.heard = ""
        self.reply = ""
        self.action = ""
        self.paused = False
        self.provider = ""
        self.provider_latency = 0.0
        self.session_id = ""
        self.confirmation = None
        self.reminder = None
        self.wake_reason = ""
        self.end_reason = ""
        self.error = ""
        self.mic_error = ""
        self.progress = ""
        self.messages = []
        self.message_version = 0
        self.tasks = []
        self.tasks_version = 0
        self.version = 0

    def add_message(self, role, text):
        if not text:
            return
        with self._lock:
            self.messages.append({"role": role, "text": str(text), "time": time.time()})
            self.messages = self.messages[-200:]
            self.message_version += 1
            self.version += 1

    def clear_messages(self):
        with self._lock:
            self.messages = []
            self.message_version += 1
            self.version += 1

    def set(self, **fields):
        with self._lock:
            for key, value in fields.items():
                if hasattr(self, key):
                    setattr(self, key, value)
            if "tasks" in fields:
                self.tasks_version += 1
            self.version += 1

    def snapshot(self):
        with self._lock:
            return {key: list(value) if key in ("messages", "tasks") else value for key, value in vars(self).items()
                    if key not in ("_lock",)}


def _round_rect(canvas, x0, y0, x1, y1, radius, **kwargs):
    points = [
        x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
        x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
        x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class Island:
    PILL_H = 44
    PILL_MIN = 152
    PILL_MAX = 720
    PANEL_W = 700

    def __init__(self, state, on_quit=None, on_toggle_pause=None, on_test=None,
                 session=None, runtime=None):
        self.state = state
        self.session = session
        self.runtime = runtime
        self._callbacks = queue.Queue()
        self._external_window = 0
        self._last_home_refresh = 0
        self._chat_version = -1
        self._confirmation_id = None
        self._tasks_version = -1
        self._task_widgets = {}
        self.on_quit = on_quit
        self.on_toggle_pause = on_toggle_pause
        self.on_test = on_test or {}
        self.accent = ACCENTS.get(cfg.UI_ACCENT, ACCENTS["cyan"])
        self.expanded = False
        self._closing = False
        self._last_version = -1
        self._width = self.PILL_MIN
        self._tab = "chat"
        self.fields = {}
        self.nav_buttons = {}
        self.pages = {}
        self._skill_vars = {}

        self.root = tk.Tk()
        self.root.title("%s — Command Center" % cfg.NAME)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=CHROMA)
        try:
            self.root.attributes("-transparentcolor", CHROMA)
        except tk.TclError:
            pass
        self._configure_ttk()

        self.canvas = tk.Canvas(self.root, bg=CHROMA, highlightthickness=0,
                                width=self.PILL_MAX, height=self.PILL_H)
        self.canvas.pack(side=tk.TOP)
        self.panel = tk.Frame(self.root, bg=P.bg_primary,
                              highlightbackground=P.border_primary, highlightthickness=1)
        self._build_pill()
        self._build_command_center()
        self._place()
        self._bind()

    def _configure_ttk(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Jarvis.TCombobox", fieldbackground=P.bg_tertiary,
                        background=P.bg_tertiary, foreground=P.text_primary,
                        arrowcolor=P.text_secondary, bordercolor=P.border_primary,
                        lightcolor=P.bg_tertiary, darkcolor=P.bg_tertiary,
                        padding=5)
        style.map("Jarvis.TCombobox", fieldbackground=[("readonly", P.bg_tertiary)],
                  foreground=[("readonly", P.text_primary)])
        style.configure("Jarvis.Vertical.TScrollbar", background=P.bg_tertiary,
                        troughcolor=P.bg_primary, bordercolor=P.bg_primary,
                        arrowcolor=P.text_secondary)

    def _place(self, height=None):
        height = height or self.PILL_H
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        margin = theme.SPACING["5"]
        if cfg.UI_POSITION == "top-right":
            x, y = screen_w - self.PILL_MAX - margin, margin
        elif cfg.UI_POSITION == "bottom-right":
            x, y = screen_w - self.PILL_MAX - margin, screen_h - height - 58
        else:
            x, y = (screen_w - self.PILL_MAX) // 2, 6
        x = max(0, x)
        y = max(0, y)
        self.root.geometry("%dx%d+%d+%d" % (self.PILL_MAX, height, x, y))

    def _bind(self):
        self.canvas.bind("<Button-1>", lambda _event: self.toggle_panel())
        self.canvas.bind("<Button-3>", self._context_menu)
        self.root.bind("<Escape>", lambda _event: self._escape())
        self.root.bind("<Control-m>", lambda _event: self.toggle_pause())
        self.root.bind("<Control-Return>", lambda _event: self._listen())
        self.menu = tk.Menu(self.root, tearoff=0, bg=P.bg_secondary, fg=P.text_primary,
                            activebackground=self.accent, activeforeground=P.text_inverse,
                            borderwidth=0)
        self.menu.add_command(label="Открыть центр", command=lambda: self.toggle_panel(True))
        self.menu.add_command(label="Пауза микрофона", command=self.toggle_pause)
        self.menu.add_command(label="Остановить голос", command=self._stop_voice)
        self.menu.add_separator()
        self.menu.add_command(label="Выход", command=self.quit)

    def _escape(self):
        if self.state.snapshot().get("confirmation") and self.runtime:
            self._confirm("нет")
        elif self.expanded:
            self.toggle_panel(False)

    def _stop_voice(self):
        if self.runtime:
            self.runtime.cancel()
        elif self.session and getattr(self.session, "voice", None):
            self.session.voice.stop()

    def call_soon(self, callback):
        self._callbacks.put(callback)

    def _track_target(self):
        if os.name != "nt":
            return
        try:
            hwnd = actions.foreground_window()
            if hwnd and actions.window_pid(hwnd) != os.getpid():
                self._external_window = hwnd
        except (AttributeError, OSError):
            pass

    def show_chat(self):
        self._track_target()
        self.toggle_panel(True)
        self.show_tab("chat")
        self.command_input.focus_set()

    def _listen(self):
        if not self.runtime:
            self.state.set(error="Это предварительный просмотр. Запустите Jarvis обычным способом.")
            return
        self._track_target()
        if self.runtime.listen(self._external_window):
            self.toggle_panel(False)

    def _send_text(self, text=None):
        value = self.command_text.get().strip() if text is None else text
        if not self.runtime:
            self.state.set(error="Это предварительный просмотр. Запустите Jarvis обычным способом.")
        elif self.runtime.submit_text(value, self._external_window,
                                      mode={"Авто": "auto", "Компьютер": "computer", "Фоновая": "background"}.get(self.chat_mode.get(), "auto")):
            if text is None:
                self.command_text.set("")
        return "break"

    def _confirm(self, answer):
        pending = self.state.snapshot().get("confirmation") or {}
        if self.runtime and pending.get("id"):
            if self.runtime.submit_confirmation(answer, pending["id"], self._external_window):
                for button in self.confirm_buttons:
                    button.configure(state="disabled")

    def _context_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    # Island ----------------------------------------------------------------
    def _build_pill(self):
        center = self.PILL_MAX // 2
        self.pill = _round_rect(
            self.canvas, center - self.PILL_MIN // 2, 2,
            center + self.PILL_MIN // 2, self.PILL_H - 2,
            (self.PILL_H - 4) // 2, fill=P.bg_primary, outline=P.border_primary,
        )
        self.ring = self.canvas.create_oval(0, 0, 0, 0, fill="", outline=P.text_tertiary, width=2)
        self.bars = [self.canvas.create_line(0, 0, 0, 0, fill=self.accent, width=2,
                                             capstyle=tk.ROUND) for _ in range(5)]
        self.status_text = self.canvas.create_text(
            center, self.PILL_H // 2, text="MUTE", fill=P.text_secondary,
            font=theme.font("xs", "bold"), anchor="w"
        )
        self.detail_text = self.canvas.create_text(
            center, self.PILL_H // 2, text="", fill=P.text_primary,
            font=theme.font("base"), anchor="w"
        )

    @staticmethod
    def _pill_points(x0, y0, x1, y1, radius):
        return [x0 + radius, y0, x1 - radius, y0, x1, y0, x1, y0 + radius,
                x1, y1 - radius, x1, y1, x1 - radius, y1, x0 + radius, y1,
                x0, y1, x0, y1 - radius, x0, y0 + radius, x0, y0]

    def _draw_pill(self, snap):
        status = snap.get("status", "idle")
        label = PILL_STATUS_LABELS.get(status, STATUS_LABELS.get(status, status.upper()))
        detail = snap.get("heard") or snap.get("reply") or snap.get("action") or ""
        if status in ("idle", "paused", "starting"):
            detail = ""
        # Segoe UI Cyrillic capitals are wider than the old Latin estimate.
        # Keep an explicit safe metric so status and detail never collide.
        label_width = max(48, len(label) * 8.2)
        detail_width = min(77, len(detail)) * 7.0
        target = self.PILL_MIN if not detail else min(
            self.PILL_MAX - 18, max(270, 62 + label_width + detail_width)
        )
        if cfg.REDUCED_MOTION:
            self._width = target
        else:
            self._width += (target - self._width) * 0.42
        center, half = self.PILL_MAX // 2, self._width / 2
        self.canvas.coords(
            self.pill,
            *self._pill_points(center - half, 2, center + half, self.PILL_H - 2,
                               (self.PILL_H - 4) / 2),
        )
        color = theme.STATE_COLORS.get(status, self.accent)
        left = center - half + 18
        active = status in ("listening", "hearing", "thinking", "speaking")
        if active:
            self.canvas.itemconfig(self.ring, state="hidden")
            level = snap.get("level", 0.0) if status in ("listening", "hearing") else 0.46
            for index, bar in enumerate(self.bars):
                phase = 0 if cfg.REDUCED_MOTION else abs(((time.time() * 5 + index) % 4) - 2) / 2
                height = 5 + max(0.12, level * (0.6 + phase * 0.4)) * 14
                x = left + index * 5
                self.canvas.itemconfig(bar, state="normal", fill=color)
                self.canvas.coords(bar, x, self.PILL_H / 2 - height / 2,
                                   x, self.PILL_H / 2 + height / 2)
            left += 34
        else:
            for bar in self.bars:
                self.canvas.itemconfig(bar, state="hidden")
            self.canvas.itemconfig(self.ring, state="normal", outline=color)
            self.canvas.coords(self.ring, left, self.PILL_H / 2 - 5,
                               left + 10, self.PILL_H / 2 + 5)
            left += 20
        self.canvas.itemconfig(self.status_text, text=label, fill=color)
        self.canvas.coords(self.status_text, left, self.PILL_H / 2)
        status_width = label_width
        self.canvas.itemconfig(self.detail_text, text=(detail[:76] + "…") if len(detail) > 77 else detail,
                               fill=P.text_primary)
        self.canvas.coords(self.detail_text, left + status_width + 14, self.PILL_H / 2)

    # Command Center --------------------------------------------------------
    def _build_command_center(self):
        shell = tk.Frame(self.panel, bg=P.bg_primary)
        shell.pack(fill=tk.BOTH, expand=True)
        sidebar = tk.Frame(shell, bg=P.bg_secondary, width=164)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        brand = tk.Frame(sidebar, bg=P.bg_secondary)
        brand.pack(fill=tk.X, padx=16, pady=(18, 16))
        tk.Label(brand, text="JARVIS", bg=P.bg_secondary, fg=P.text_primary,
                 font=theme.font("lg", "bold")).pack(anchor="w")
        tk.Label(brand, text="2.1  /  ASSISTANT", bg=P.bg_secondary,
                 fg=self.accent, font=theme.font("xs", "bold")).pack(anchor="w", pady=(2, 0))

        nav = (
            ("chat", "Разговор"), ("tasks", "Задачи"), ("home", "Состояние"), ("activation", "Активация"),
            ("providers", "AI Pool"), ("speech", "Голос и речь"),
            ("skills", "Навыки"), ("routines", "Рутины"),
            ("memory", "Память"), ("diagnostics", "Диагностика"),
        )
        for key, title in nav:
            button = tk.Button(
                sidebar, text=title, command=lambda item=key: self.show_tab(item),
                anchor="w", relief=tk.FLAT, bd=0, padx=16, pady=9,
                bg=P.bg_secondary, fg=P.text_secondary,
                activebackground=P.bg_tertiary, activeforeground=P.text_primary,
                font=theme.font("base"), cursor="hand2",
            )
            button.pack(fill=tk.X, padx=8, pady=1)
            self.nav_buttons[key] = button

        footer = tk.Frame(sidebar, bg=P.bg_secondary)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=16, pady=14)
        self.sidebar_state = tk.Label(footer, text="● MUTE", bg=P.bg_secondary,
                                      fg=P.text_tertiary, font=theme.font("xs", "bold"))
        self.sidebar_state.pack(anchor="w")
        tk.Label(footer, text="Ctrl+Alt+Space\nГоворить", bg=P.bg_secondary, justify="left", anchor="w",
                 fg=P.text_tertiary, font=theme.font("xs")).pack(anchor="w", pady=(4, 0))
        self._button(footer, "Свернуть", lambda: self.toggle_panel(False)).pack(fill=tk.X, pady=(10, 4))
        self._button(footer, "Выход", self.quit).pack(fill=tk.X)

        main = tk.Frame(shell, bg=P.bg_primary)
        main.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        header = tk.Frame(main, bg=P.bg_primary)
        header.pack(fill=tk.X, padx=20, pady=(16, 10))
        self.page_title = tk.Label(header, text="Главная", bg=P.bg_primary,
                                   fg=P.text_primary, font=theme.font("xl", "bold"))
        self.page_title.pack(side=tk.LEFT)
        self.header_status = tk.Label(header, text="", bg=P.bg_tertiary,
                                      fg=P.text_secondary, padx=10, pady=5,
                                      font=theme.font("xs", "bold"))
        self.header_status.pack(side=tk.RIGHT)

        self.body = tk.Frame(main, bg=P.bg_primary)
        self.body.pack(fill=tk.BOTH, expand=True, padx=20, pady=(0, 16))
        self.pages = {
            "chat": self._page_chat(),
            "tasks": self._page_tasks(),
            "home": self._page_home(),
            "activation": self._settings_page(("activation", "behaviour")),
            "providers": self._page_providers(),
            "speech": self._settings_page(("speech", "look")),
            "skills": self._page_skills(),
            "routines": self._page_routines(),
            "memory": self._page_memory(),
            "diagnostics": self._page_diagnostics(),
        }
        self.show_tab("chat")

    def _page_chat(self):
        page = tk.Frame(self.body, bg=P.bg_primary)
        tk.Label(page, text="Напишите просьбу или нажмите «Говорить».",
                 bg=P.bg_primary, fg=P.text_secondary, anchor="w",
                 font=theme.font("base")).pack(fill=tk.X, pady=(0, 8))
        self.chat_status = tk.Label(page, text="", bg=P.bg_primary, fg=self.accent,
                                    anchor="w", justify="left", wraplength=460,
                                    font=theme.font("sm"))
        self.chat_status.pack(fill=tk.X, pady=(0, 8))
        conversation = tk.Frame(page, bg=P.bg_secondary,
                                highlightthickness=1, highlightbackground=P.border_primary)
        conversation.pack(fill=tk.BOTH, expand=True)
        self.chat_log = tk.Text(conversation, wrap=tk.WORD, bg=P.bg_secondary,
                                fg=P.text_primary, relief=tk.FLAT, padx=16, pady=14,
                                font=theme.font("md"), spacing3=12, cursor="arrow",
                                state="disabled", height=12, width=30)
        scrollbar = ttk.Scrollbar(conversation, orient="vertical", command=self.chat_log.yview,
                                  style="Jarvis.Vertical.TScrollbar")
        self.chat_log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.chat_log.tag_configure("user_name", foreground=self.accent, font=theme.font("sm", "bold"))
        self.chat_log.tag_configure("assistant_name", foreground=P.success, font=theme.font("sm", "bold"))
        self.chat_log.tag_configure("hint", foreground=P.text_secondary)
        self.confirm_row = tk.Frame(page, bg=P.bg_primary)
        self.confirm_label = tk.Label(self.confirm_row, text="Подтвердить действие?",
                                      bg=P.bg_primary, fg=P.warning, anchor="w",
                                      wraplength=450, font=theme.font("sm"))
        self.confirm_label.pack(fill=tk.X, pady=4)
        choices = tk.Frame(self.confirm_row, bg=P.bg_primary)
        choices.pack(fill=tk.X)
        self.confirm_buttons = [
            self._button(choices, "Подтвердить", lambda: self._confirm("да"), primary=True),
            self._button(choices, "Отменить", lambda: self._confirm("нет")),
        ]
        self.confirm_buttons[0].pack(side=tk.LEFT)
        self.confirm_buttons[1].pack(side=tk.LEFT, padx=6)
        composer = tk.Frame(page, bg=P.bg_primary)
        composer.pack(side=tk.BOTTOM, fill=tk.X, pady=(12, 0))
        mode_row = tk.Frame(composer, bg=P.bg_primary)
        mode_row.pack(fill=tk.X, pady=(0, 6))
        self.chat_mode = tk.StringVar(value="Авто")
        ttk.Combobox(mode_row, textvariable=self.chat_mode, values=("Авто", "Компьютер", "Фоновая"),
                     state="readonly", width=13, style="Jarvis.TCombobox").pack(side=tk.LEFT)
        self._button(mode_row, "Задачи", lambda: self.show_tab("tasks")).pack(side=tk.RIGHT)
        self.command_text = tk.StringVar()
        self.command_input = tk.Entry(composer, textvariable=self.command_text, relief=tk.FLAT,
                                      bg=P.bg_tertiary, fg=P.text_primary, insertbackground=self.accent,
                                      font=theme.font("md"), highlightthickness=1,
                                      highlightbackground=P.border_primary, highlightcolor=self.accent)
        self.command_input.pack(fill=tk.X, ipady=10)
        self.command_input.bind("<Return>", lambda _event: self._send_text())
        row = tk.Frame(composer, bg=P.bg_primary)
        row.pack(fill=tk.X, pady=(9, 0))
        self._button(row, "Отправить", self._send_text, primary=True).pack(side=tk.LEFT)
        self._button(row, "Говорить", self._listen).pack(side=tk.LEFT, padx=6)
        self._button(row, "Стоп", self._stop_voice, danger=True).pack(side=tk.LEFT)
        self._button(row, "Новый", lambda: self.runtime.new_conversation() if self.runtime else None).pack(side=tk.RIGHT)
        return page

    def _page_tasks(self):
        page, inner = self._scroll_page()
        tk.Label(inner, text="Одна задача управляет экраном. Две фоновые могут готовить ответы параллельно.",
                 bg=P.bg_primary, fg=P.text_secondary, justify="left", wraplength=450,
                 font=theme.font("sm")).pack(fill=tk.X, pady=(0, 8))
        tk.Label(inner, text="В режиме «Компьютер» содержимое и снимки выбранного окна доступны вашему AI.",
                 bg=P.bg_primary, fg=P.text_tertiary, justify="left", wraplength=450,
                 font=theme.font("sm")).pack(fill=tk.X, pady=(0, 10))
        self.task_goal = tk.StringVar()
        entry = tk.Entry(inner, textvariable=self.task_goal, bg=P.bg_tertiary,
                         fg=P.text_primary, insertbackground=self.accent, relief=tk.FLAT,
                         font=theme.font("base"))
        entry.pack(fill=tk.X, ipady=9)
        entry.bind("<Return>", lambda _event: self._submit_task())
        row = tk.Frame(inner, bg=P.bg_primary)
        row.pack(fill=tk.X, pady=8)
        self.task_kind = tk.StringVar(value="Компьютер")
        ttk.Combobox(row, textvariable=self.task_kind, values=("Компьютер", "Фоновая"),
                     state="readonly", width=13, style="Jarvis.TCombobox").pack(side=tk.LEFT)
        self._button(row, "Добавить", self._submit_task, primary=True).pack(side=tk.LEFT, padx=6)
        self._button(row, "Стоп всё", self._stop_voice, danger=True).pack(side=tk.RIGHT)
        self.task_empty = tk.Label(inner, text="Здесь появятся ваши задачи и их результаты.",
                                    bg=P.bg_primary, fg=P.text_tertiary, wraplength=450,
                                    font=theme.font("base"), pady=24)
        self.task_empty.pack(fill=tk.X)
        self.task_list = tk.Frame(inner, bg=P.bg_primary)
        self.task_list.pack(fill=tk.BOTH, expand=True)
        return page

    def _submit_task(self):
        if not self.runtime:
            return "break"
        kind = "background" if self.task_kind.get() == "Фоновая" else "computer"
        if self.runtime.submit_task(self.task_goal.get(), kind, self._external_window):
            self.task_goal.set("")
        return "break"

    def _task_action(self, task_id, action, confirmation_id=None, reply=None):
        manager = self.runtime.task_manager if self.runtime else None
        if not manager:
            return
        if action == "cancel":
            manager.cancel(task_id)
        elif action == "pause":
            manager.pause(task_id)
        elif action == "resume":
            manager.resume(task_id, user_reply=reply)
        else:
            manager.resume(task_id, confirmation_id=confirmation_id, approved=action == "yes")

    def _refresh_tasks(self, snap):
        if snap["tasks_version"] == self._tasks_version:
            return
        self._tasks_version = snap["tasks_version"]
        items = snap["tasks"]
        self.task_empty.pack_forget() if items else self.task_empty.pack(fill=tk.X, before=self.task_list)
        active_ids = {item["id"] for item in items}
        for old in set(self._task_widgets) - active_ids:
            self._task_widgets.pop(old)["frame"].destroy()
        labels = {"queued": "В ОЧЕРЕДИ", "running": "ВЫПОЛНЯЕТСЯ", "completed": "ГОТОВО",
                  "failed": "ОСТАНОВЛЕНА", "cancelled": "ОТМЕНЕНА", "paused": "ПАУЗА",
                  "pausing": "ПАУЗА…", "cancelling": "ОСТАНОВКА…",
                  "waiting_confirmation": "ПОДТВЕРЖДЕНИЕ", "waiting_input": "НУЖЕН ОТВЕТ"}
        for task in items:
            task_id, status = task["id"], task["state"]
            if task_id not in self._task_widgets:
                frame = self._card(self.task_list)
                head = tk.Label(frame, bg=P.bg_secondary, anchor="w", font=theme.font("xs", "bold"))
                head.pack(fill=tk.X, padx=12, pady=(10, 4))
                goal = tk.Label(frame, bg=P.bg_secondary, fg=P.text_primary, justify="left",
                                anchor="w", wraplength=425, font=theme.font("base", "bold"))
                goal.pack(fill=tk.X, padx=12)
                progress = tk.Label(frame, bg=P.bg_secondary, fg=P.text_secondary, justify="left",
                                    anchor="w", wraplength=425, font=theme.font("sm"))
                progress.pack(fill=tk.X, padx=12, pady=8)
                controls = tk.Frame(frame, bg=P.bg_secondary)
                controls.pack(fill=tk.X, padx=12, pady=(0, 10))
                pause = self._button(controls, "Пауза", lambda t=task_id: self._task_action(t, "pause"))
                cancel = self._button(controls, "Отменить", lambda t=task_id: self._task_action(t, "cancel"), danger=True)
                yes = self._button(controls, "Подтвердить", lambda: None, primary=True)
                no = self._button(controls, "Нет", lambda: None)
                reply = tk.StringVar()
                reply_entry = tk.Entry(frame, textvariable=reply, bg=P.bg_tertiary,
                                       fg=P.text_primary, insertbackground=self.accent, font=theme.font("base"))
                self._task_widgets[task_id] = dict(frame=frame, head=head, goal=goal, progress=progress,
                                                    pause=pause, cancel=cancel, yes=yes, no=no,
                                                    reply=reply, reply_entry=reply_entry)
            widgets = self._task_widgets[task_id]
            widgets["frame"].pack_forget()
            widgets["frame"].pack(fill=tk.X, pady=5)
            widgets["head"].configure(text="%s  ·  %s  ·  %s" % (task_id, labels.get(status, status),
                                                                 "ФОН" if task["kind"] == "background" else "ЭКРАН"),
                                       fg=P.success if status == "completed" else P.warning if status in ("waiting_confirmation", "waiting_input") else self.accent)
            widgets["goal"].configure(text=task["goal"][:240])
            widgets["progress"].configure(text=(task.get("message") if status in ("completed", "failed") else task["progress"])[:700])
            for name in ("pause", "cancel", "yes", "no", "reply_entry"):
                widgets[name].pack_forget()
            if status not in ("completed", "failed", "cancelled"):
                widgets["cancel"].pack(side=tk.RIGHT)
            if status in ("queued", "running") and task["kind"] == "computer":
                widgets["pause"].configure(text="Пауза", command=lambda t=task_id: self._task_action(t, "pause"))
                widgets["pause"].pack(side=tk.LEFT)
            elif status == "paused":
                widgets["pause"].configure(text="Продолжить", command=lambda t=task_id: self._task_action(t, "resume"))
                widgets["pause"].pack(side=tk.LEFT)
            elif status == "waiting_confirmation":
                pending = task.get("pending") or {}
                widgets["yes"].configure(text="Подтвердить", command=lambda t=task_id, c=pending.get("id"): self._task_action(t, "yes", c))
                widgets["no"].configure(command=lambda t=task_id, c=pending.get("id"): self._task_action(t, "no", c))
                widgets["yes"].pack(side=tk.LEFT)
                widgets["no"].pack(side=tk.LEFT, padx=5)
            elif status == "waiting_input":
                widgets["reply_entry"].pack(fill=tk.X, padx=12, pady=(0, 10))
                widgets["yes"].configure(text="Ответить", command=lambda t=task_id, w=widgets: self._task_action(t, "resume", reply=w["reply"].get()))
                widgets["yes"].pack(side=tk.LEFT)

    def _refresh_chat(self, snap):
        if snap["message_version"] != self._chat_version:
            self._chat_version = snap["message_version"]
            position = self.chat_log.yview()
            at_end = position[1] >= 0.98
            self.chat_log.configure(state="normal")
            self.chat_log.delete("1.0", "end")
            if not snap["messages"]:
                self.chat_log.insert("end", "Джарвис на связи.\n\n", "assistant_name")
                self.chat_log.insert("end", "Можно начать так:\n\n"
                                     "«Какие окна открыты?»\n"
                                     "«Открой блокнот и напечатай Привет, мир!»\n"
                                     "«Напомни через 10 минут сделать перерыв»\n\n"
                                     "Для ввода в другую программу сначала выберите её окно, "
                                     "затем вернитесь сюда через Ctrl+Alt+J.", "hint")
            for message in snap["messages"]:
                user = message["role"] == "user"
                self.chat_log.insert("end", "ВЫ\n" if user else "ДЖАРВИС\n",
                                     "user_name" if user else "assistant_name")
                self.chat_log.insert("end", message["text"] + "\n\n")
            self.chat_log.configure(state="disabled")
            if at_end:
                self.chat_log.see("end")
            else:
                self.chat_log.yview_moveto(position[0])
        error = snap.get("error") or snap.get("mic_error")
        if error:
            status = error
        elif snap.get("progress"):
            status = snap["progress"]
        elif snap.get("status") in ("listening", "hearing"):
            status = "Слушаю… " + (snap.get("heard") or "Говорите обычным голосом.")
        else:
            status = "Микрофон на паузе · чат работает" if snap.get("paused") else "Голос: Ctrl+Alt+Space · остановить: Ctrl+Alt+X"
        self.chat_status.configure(text=status, fg=P.error if error else self.accent)
        if snap.get("confirmation") and snap["confirmation"].get("expires_at", time.time() + 1) > time.time():
            self.confirm_row.pack(fill=tk.X, pady=(6, 0), before=self.command_input.master)
            confirmation = snap["confirmation"]
            if self._confirmation_id != confirmation.get("id"):
                self._confirmation_id = confirmation.get("id")
                for button in self.confirm_buttons:
                    button.configure(state="normal")
            self.confirm_label.configure(text=confirmation.get("description", "Подтвердить действие?"))
        else:
            self._confirmation_id = None
            self.confirm_row.pack_forget()

    def _scroll_page(self):
        page = tk.Frame(self.body, bg=P.bg_primary)
        canvas = tk.Canvas(page, bg=P.bg_primary, highlightthickness=0)
        scrollbar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview,
                                  style="Jarvis.Vertical.TScrollbar")
        inner = tk.Frame(canvas, bg=P.bg_primary)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(int(-event.delta / 120), "units"))
        return page, inner

    def _section_label(self, parent, text, note=""):
        row = tk.Frame(parent, bg=P.bg_primary)
        row.pack(fill=tk.X, pady=(14, 7))
        tk.Label(row, text=text.upper(), bg=P.bg_primary, fg=self.accent,
                 font=theme.font("xs", "bold")).pack(side=tk.LEFT)
        if note:
            tk.Label(row, text=note, bg=P.bg_primary, fg=P.text_tertiary,
                     font=theme.font("xs")).pack(side=tk.RIGHT)

    def _card(self, parent, title="", note=""):
        frame = tk.Frame(parent, bg=P.bg_secondary,
                         highlightbackground=P.border_secondary, highlightthickness=1)
        frame.pack(fill=tk.X, pady=5)
        if title:
            head = tk.Frame(frame, bg=P.bg_secondary)
            head.pack(fill=tk.X, padx=14, pady=(12, 6))
            tk.Label(head, text=title, bg=P.bg_secondary, fg=P.text_primary,
                     font=theme.font("base", "bold")).pack(side=tk.LEFT)
            if note:
                tk.Label(head, text=note, bg=P.bg_secondary, fg=P.text_tertiary,
                         font=theme.font("xs")).pack(side=tk.RIGHT)
        return frame

    def _button(self, parent, text, command, primary=False, danger=False):
        bg = self.accent if primary else P.bg_tertiary
        fg = P.text_inverse if primary else P.error if danger else P.text_primary
        return tk.Button(parent, text=text, command=command, relief=tk.FLAT, bd=0,
                         bg=bg, fg=fg, activebackground=P.accent_hover if primary else P.border_primary,
                         activeforeground=P.text_inverse if primary else fg,
                         font=theme.font("sm", "bold"), padx=12, pady=7, cursor="hand2")

    def _page_home(self):
        page, inner = self._scroll_page()
        self._section_label(inner, "Система", "голос + чат + Windows")
        hero = self._card(inner)
        top = tk.Frame(hero, bg=P.bg_secondary)
        top.pack(fill=tk.X, padx=16, pady=14)
        self.home_state = tk.Label(top, text="MUTE", bg=P.bg_secondary, fg=P.text_tertiary,
                                   font=theme.font("2xl", "bold"))
        self.home_state.pack(anchor="w")
        self.home_path = tk.Label(
            top, text="Обратитесь по имени или нажмите «Говорить».",
            bg=P.bg_secondary, fg=P.text_secondary, font=theme.font("sm"),
        )
        self.home_path.pack(anchor="w", pady=(4, 0))
        actions = tk.Frame(hero, bg=P.bg_secondary)
        actions.pack(fill=tk.X, padx=16, pady=(0, 14))
        self._button(actions, "Пауза микрофона", self.toggle_pause).pack(side=tk.LEFT)
        self._button(actions, "Проверить wake", lambda: self.run_test("wake"), primary=True).pack(side=tk.LEFT, padx=7)

        self._section_label(inner, "Состояние")
        grid = tk.Frame(inner, bg=P.bg_primary)
        grid.pack(fill=tk.X)
        self.home_metrics = {}
        for index, (key, title) in enumerate((
            ("mic", "МИКРОФОН"), ("provider", "AI PATH"),
            ("memory", "ПАМЯТЬ"), ("reminders", "НАПОМИНАНИЯ"),
        )):
            card = tk.Frame(grid, bg=P.bg_secondary,
                            highlightbackground=P.border_secondary, highlightthickness=1)
            card.grid(row=index // 2, column=index % 2, sticky="nsew", padx=(0 if index % 2 == 0 else 5,
                                                                            5 if index % 2 == 0 else 0), pady=5)
            grid.grid_columnconfigure(index % 2, weight=1)
            tk.Label(card, text=title, bg=P.bg_secondary, fg=P.text_tertiary,
                     font=theme.font("xs", "bold")).pack(anchor="w", padx=12, pady=(10, 4))
            value = tk.Label(card, text="—", bg=P.bg_secondary, fg=P.text_primary,
                             font=theme.font("md", "bold"))
            value.pack(anchor="w", padx=12, pady=(0, 11))
            self.home_metrics[key] = value

        self._section_label(inner, "Последние действия")
        self.home_actions = tk.Label(inner, text="Действий пока нет.", bg=P.bg_secondary,
                                     fg=P.text_secondary, justify="left", anchor="nw",
                                     font=theme.font("sm"), padx=14, pady=12, wraplength=455)
        self.home_actions.pack(fill=tk.X, pady=(0, 8))
        return page

    def _settings_page(self, groups):
        page, inner = self._scroll_page()
        values = cfg.current_values()
        for group in groups:
            title = dict(cfg.GROUPS).get(group, group)
            self._section_label(inner, title)
            for spec in cfg.SPECS:
                if spec.group != group:
                    continue
                card = self._card(inner)
                row = tk.Frame(card, bg=P.bg_secondary)
                row.pack(fill=tk.X, padx=12, pady=(10, 5))
                tk.Label(row, text=spec.label, bg=P.bg_secondary, fg=P.text_primary,
                         font=theme.font("sm", "bold"), width=25, anchor="w").pack(side=tk.LEFT)
                variable = self.fields.get(spec.key) or tk.StringVar(value=values.get(spec.key, spec.default))
                self.fields[spec.key] = variable
                if spec.kind == "bool":
                    widget = tk.Checkbutton(
                        row, variable=variable, onvalue="true", offvalue="false",
                        bg=P.bg_secondary, fg=P.text_primary, selectcolor=P.bg_tertiary,
                        activebackground=P.bg_secondary, highlightthickness=0, bd=0,
                    )
                elif spec.kind == "choice":
                    widget = ttk.Combobox(row, textvariable=variable, values=list(spec.choices),
                                          state="readonly", width=24, style="Jarvis.TCombobox")
                else:
                    widget = tk.Entry(row, textvariable=variable, bg=P.bg_tertiary,
                                      fg=P.text_primary, insertbackground=P.text_primary,
                                      relief=tk.FLAT, width=27,
                                      show="•" if spec.kind == "secret" else "")
                widget.pack(side=tk.RIGHT)
                if spec.hint:
                    tk.Label(card, text=spec.hint, bg=P.bg_secondary, fg=P.text_tertiary,
                             font=theme.font("xs"), wraplength=450, justify="left").pack(
                        anchor="w", padx=12, pady=(0, 10))
        footer = tk.Frame(inner, bg=P.bg_primary)
        footer.pack(fill=tk.X, pady=(12, 20))
        note = tk.Label(footer, text="", bg=P.bg_primary, fg=P.text_secondary,
                        font=theme.font("xs"))
        note.pack(side=tk.LEFT)
        self._button(footer, "Сохранить", lambda: self.save_settings(note), primary=True).pack(side=tk.RIGHT)
        return page

    def _page_providers(self):
        page, inner = self._scroll_page()
        self._section_label(inner, "AI Pool", "fallback по здоровью и лимитам")
        values = cfg.current_values()
        for name in ("groq", "cerebras", "gemini", "openrouter", "claude"):
            info = ai.PROVIDERS[name]
            card = self._card(inner, info.label, ", ".join(info.capabilities))
            body = tk.Frame(card, bg=P.bg_secondary)
            body.pack(fill=tk.X, padx=14, pady=(0, 12))
            key_name = "JARVIS_%s_KEY" % name.upper()
            variable = self.fields.get(key_name) or tk.StringVar(value=values.get(key_name, ""))
            self.fields[key_name] = variable
            entry = tk.Entry(body, textvariable=variable, show="•", bg=P.bg_tertiary,
                             fg=P.text_primary, insertbackground=P.text_primary,
                             relief=tk.FLAT, width=30)
            entry.pack(side=tk.LEFT, ipady=5)
            self._button(body, "Тест", lambda provider=name: self._test_provider(provider)).pack(side=tk.RIGHT)
            status = tk.Label(card, text="не проверен", bg=P.bg_secondary,
                              fg=P.text_tertiary, font=theme.font("xs"))
            status.pack(anchor="w", padx=14, pady=(0, 10))
            setattr(self, "provider_status_" + name, status)
        footer = tk.Frame(inner, bg=P.bg_primary)
        footer.pack(fill=tk.X, pady=(12, 20))
        self.provider_note = tk.Label(footer, text="Ключи защищаются Windows DPAPI.",
                                      bg=P.bg_primary, fg=P.text_secondary,
                                      font=theme.font("xs"))
        self.provider_note.pack(side=tk.LEFT)
        self._button(footer, "Сохранить ключи", lambda: self.save_settings(self.provider_note),
                     primary=True).pack(side=tk.RIGHT)
        return page

    def _page_skills(self):
        page, inner = self._scroll_page()
        self._section_label(inner, "Навыки и разрешения", "%d навыков" % len(skills.SKILLS))
        for item in skills.metadata():
            card = self._card(inner)
            row = tk.Frame(card, bg=P.bg_secondary)
            row.pack(fill=tk.X, padx=12, pady=9)
            text = tk.Frame(row, bg=P.bg_secondary)
            text.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Label(text, text=item["name"], bg=P.bg_secondary, fg=P.text_primary,
                     font=theme.font("sm", "bold")).pack(anchor="w")
            tk.Label(text, text="%s  ·  risk %d" % (item["about"], item["risk"]),
                     bg=P.bg_secondary, fg=P.text_tertiary,
                     font=theme.font("xs"), wraplength=300, justify="left").pack(anchor="w")
            enabled = tk.BooleanVar(value=item["enabled"])
            confirm = tk.BooleanVar(value=item["always_confirm"])
            self._skill_vars[item["name"]] = (enabled, confirm)
            tk.Checkbutton(row, text="вкл", variable=enabled, bg=P.bg_secondary,
                           fg=P.text_secondary, selectcolor=P.bg_tertiary,
                           activebackground=P.bg_secondary, bd=0).pack(side=tk.LEFT)
            tk.Checkbutton(row, text="всегда спросить", variable=confirm, bg=P.bg_secondary,
                           fg=P.text_secondary, selectcolor=P.bg_tertiary,
                           activebackground=P.bg_secondary, bd=0).pack(side=tk.LEFT, padx=(5, 0))
        footer = tk.Frame(inner, bg=P.bg_primary)
        footer.pack(fill=tk.X, pady=(12, 20))
        self.skill_note = tk.Label(footer, text="", bg=P.bg_primary, fg=P.text_secondary,
                                   font=theme.font("xs"))
        self.skill_note.pack(side=tk.LEFT)
        self._button(footer, "Сохранить разрешения", self._save_permissions,
                     primary=True).pack(side=tk.RIGHT)
        return page

    def _page_routines(self):
        page, inner = self._scroll_page()
        self._section_label(inner, "Рутины", "типизированные шаги")
        form = self._card(inner, "Новая или обновлённая рутина")
        self.routine_name = tk.StringVar()
        self.routine_trigger = tk.StringVar()
        for label, variable in (("Название", self.routine_name), ("Голосовой триггер", self.routine_trigger)):
            row = tk.Frame(form, bg=P.bg_secondary)
            row.pack(fill=tk.X, padx=12, pady=5)
            tk.Label(row, text=label, bg=P.bg_secondary, fg=P.text_secondary,
                     font=theme.font("sm"), width=18, anchor="w").pack(side=tk.LEFT)
            tk.Entry(row, textvariable=variable, bg=P.bg_tertiary, fg=P.text_primary,
                     insertbackground=P.text_primary, relief=tk.FLAT).pack(side=tk.LEFT, fill=tk.X,
                                                                           expand=True, ipady=5)
        tk.Label(form, text="Шаги JSON: [{\"skill\":\"open_app\",\"params\":{\"name\":\"telegram\"}}]",
                 bg=P.bg_secondary, fg=P.text_tertiary, font=theme.font("xs"),
                 wraplength=430, justify="left").pack(anchor="w", padx=12, pady=(8, 4))
        self.routine_steps = tk.Text(form, height=5, bg=P.bg_tertiary, fg=P.text_primary,
                                     insertbackground=P.text_primary, relief=tk.FLAT,
                                     font=theme.font("sm", mono=True), wrap="word")
        self.routine_steps.pack(fill=tk.X, padx=12, pady=(0, 8))
        self.routine_steps.insert("1.0", "[]")
        buttons = tk.Frame(form, bg=P.bg_secondary)
        buttons.pack(fill=tk.X, padx=12, pady=(0, 12))
        self.routine_note = tk.Label(buttons, text="", bg=P.bg_secondary, fg=P.text_secondary,
                                     font=theme.font("xs"))
        self.routine_note.pack(side=tk.LEFT)
        self._button(buttons, "Сохранить", self._save_routine, primary=True).pack(side=tk.RIGHT)

        self._section_label(inner, "Сохранённые")
        self.routine_list = tk.Frame(inner, bg=P.bg_primary)
        self.routine_list.pack(fill=tk.X)
        self._refresh_routines()
        return page

    def _page_memory(self):
        page, inner = self._scroll_page()
        self._section_label(inner, "Память и приватность", "аудио не сохраняется")
        note_card = self._card(inner, "Быстрая заметка")
        row = tk.Frame(note_card, bg=P.bg_secondary)
        row.pack(fill=tk.X, padx=12, pady=12)
        self.note_input = tk.StringVar()
        tk.Entry(row, textvariable=self.note_input, bg=P.bg_tertiary, fg=P.text_primary,
                 insertbackground=P.text_primary, relief=tk.FLAT).pack(side=tk.LEFT, fill=tk.X,
                                                                       expand=True, ipady=6)
        self._button(row, "Добавить", self._add_note).pack(side=tk.LEFT, padx=(8, 0))
        self.memory_text = tk.Label(inner, text="", bg=P.bg_secondary, fg=P.text_secondary,
                                    font=theme.font("sm"), justify="left", anchor="nw",
                                    padx=14, pady=12, wraplength=455)
        self.memory_text.pack(fill=tk.X, pady=6)
        buttons = tk.Frame(inner, bg=P.bg_primary)
        buttons.pack(fill=tk.X, pady=(8, 20))
        self._button(buttons, "Обновить", self._refresh_memory).pack(side=tk.LEFT)
        self._button(buttons, "Забыть выученные команды", self._clear_learned,
                     danger=True).pack(side=tk.RIGHT)
        self._refresh_memory()
        return page

    def _page_diagnostics(self):
        page = tk.Frame(self.body, bg=P.bg_primary)
        controls = tk.Frame(page, bg=P.bg_primary)
        controls.pack(fill=tk.X, pady=(2, 10))
        for title, key in (("Готовность", "ready"), ("Wake", "wake"),
                           ("Микрофон", "mic"), ("Голос", "voice"),
                           ("AI Pool", "brain"), ("Устройства", "devices")):
            self._button(controls, title, lambda item=key: self.run_test(item)).pack(side=tk.LEFT, padx=(0, 5))
        self.check_output = tk.Text(page, bg=P.bg_secondary, fg=P.text_primary,
                                    insertbackground=P.text_primary, relief=tk.FLAT,
                                    font=theme.font("sm", mono=True), wrap="word", padx=12, pady=12)
        self.check_output.pack(fill=tk.BOTH, expand=True)
        self.check_output.insert("1.0", "Выберите проверку. Никакой тест wake не отправляет звук в облако.")
        self.check_output.configure(state="disabled")
        return page

    # Page actions ----------------------------------------------------------
    def show_tab(self, name):
        titles = {"chat": "Разговор", "tasks": "Задачи", "home": "Состояние", "activation": "Активация", "providers": "AI Pool",
                  "speech": "Голос и речь", "skills": "Навыки", "routines": "Рутины",
                  "memory": "Память и приватность", "diagnostics": "Диагностика"}
        self._tab = name
        self.page_title.configure(text=titles.get(name, name))
        for key, button in self.nav_buttons.items():
            button.configure(bg=P.bg_tertiary if key == name else P.bg_secondary,
                             fg=P.text_primary if key == name else P.text_secondary)
        for page in self.pages.values():
            page.pack_forget()
        self.pages[name].pack(fill=tk.BOTH, expand=True)
        if name == "home":
            self._refresh_home()
        elif name == "memory":
            self._refresh_memory()
        elif name == "routines":
            self._refresh_routines()

    def toggle_panel(self, force=None):
        self.expanded = (not self.expanded) if force is None else bool(force)
        if self.expanded:
            self.panel.pack(fill=tk.BOTH, expand=True, padx=10, pady=(4, 0))
            height = min(680, max(380, self.root.winfo_screenheight() - self.PILL_H - 80))
            self._place(height=self.PILL_H + height)
        else:
            self.panel.pack_forget()
            self._place()

    def show_onboarding(self):
        """First-run privacy and readiness walkthrough."""
        self.toggle_panel(True)
        dialog = tk.Toplevel(self.root)
        dialog.title("Jarvis 1.0 Beta — первый запуск")
        dialog.configure(bg=P.bg_primary)
        dialog.attributes("-topmost", True)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        width, height = 540, 390
        x = max(20, (dialog.winfo_screenwidth() - width) // 2)
        y = max(20, (dialog.winfo_screenheight() - height) // 2)
        dialog.geometry("%dx%d+%d+%d" % (width, height, x, y))

        slides = [
            ("РАЗГОВОР И ГОЛОС",
             "Напишите просьбу в разговоре или нажмите «Говорить». Ctrl+Alt+Space включает голос "
             "из любого окна. Для русского имени в фоне работает локальный Whisper; "
             "до обращения аудио не отправляется в облако. Ctrl+M ставит микрофон на паузу.",
             None),
            ("МИКРОФОН",
             "Выберите устройство в разделе «Активация». Проверка микрофона распознаёт "
             "тестовую реплику локально и показывает уровень сигнала.", "mic"),
            ("WAKE-WORD",
             "Тест wake работает локально: скажите «Джарвис». Только после подтверждённого "
             "имени разрешается активное распознавание и временная session-id.", "wake"),
            ("AI POOL И ПРИВАТНОСТЬ",
             "Ключи Groq, Cerebras, Gemini и OpenRouter необязательны и защищаются Windows DPAPI. "
             "Cloud STT получает аудио только после wake и только если включено разрешение.", None),
            ("ГОТОВО",
             "Jarvis может работать локально без единого ключа. Запустите readiness-проверку, "
             "затем закройте центр — помощник перейдёт в тихий Mute.", "ready"),
        ]
        index = {"value": 0}

        step = tk.Label(dialog, text="", bg=P.bg_primary, fg=self.accent,
                        font=theme.font("xs", "bold"))
        step.pack(anchor="w", padx=28, pady=(26, 8))
        title = tk.Label(dialog, text="", bg=P.bg_primary, fg=P.text_primary,
                         font=theme.font("2xl", "bold"))
        title.pack(anchor="w", padx=28)
        body = tk.Label(dialog, text="", bg=P.bg_primary, fg=P.text_secondary,
                        font=theme.font("md"), justify="left", wraplength=470)
        body.pack(anchor="w", padx=28, pady=(16, 16))
        action = self._button(dialog, "Запустить проверку", lambda: None)
        action.pack(anchor="w", padx=28)
        footer = tk.Frame(dialog, bg=P.bg_primary)
        footer.pack(side=tk.BOTTOM, fill=tk.X, padx=28, pady=24)

        def finish():
            try:
                cfg.FIRST_RUN_MARK.write_text("1.0-beta", encoding="utf-8")
            except OSError:
                pass
            dialog.destroy()
            self.show_tab("home")

        def render():
            i = index["value"]
            heading, copy, test_key = slides[i]
            step.configure(text="ШАГ %d / %d" % (i + 1, len(slides)))
            title.configure(text=heading)
            body.configure(text=copy)
            action.configure(state=tk.NORMAL if test_key else tk.DISABLED,
                             command=(lambda key=test_key: (dialog.destroy(),
                                                            self.show_tab("diagnostics"),
                                                            self.run_test(key))) if test_key else lambda: None)
            back.configure(state=tk.NORMAL if i else tk.DISABLED)
            next_button.configure(text="Завершить" if i == len(slides) - 1 else "Дальше",
                                  command=finish if i == len(slides) - 1 else forward)

        def backward():
            index["value"] = max(0, index["value"] - 1)
            render()

        def forward():
            index["value"] = min(len(slides) - 1, index["value"] + 1)
            render()

        back = self._button(footer, "Назад", backward)
        back.pack(side=tk.LEFT)
        next_button = self._button(footer, "Дальше", forward, primary=True)
        next_button.pack(side=tk.RIGHT)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        render()

    def save_settings(self, note=None):
        values = {key: variable.get() for key, variable in self.fields.items()}
        errors = cfg.save(values)
        target = note or getattr(self, "provider_note", None)
        if errors:
            key, message = next(iter(errors.items()))
            text = "%s: %s" % (cfg.BY_KEY.get(key, key).label if key in cfg.BY_KEY else key, message)
            if target:
                target.configure(text=text, fg=P.error)
            return False
        cfg.reload()
        if self.runtime:
            self.runtime.configure()
        self._place(self.root.winfo_height())
        if target:
            target.configure(text="Сохранено. Голос применяется; оформление — при следующем запуске.",
                             fg=P.success, wraplength=440)
        return True

    def _test_provider(self, provider):
        status = getattr(self, "provider_status_" + provider)
        if not self.save_settings(self.provider_note):
            return
        status.configure(text="проверяю…", fg=P.warning)

        def worker():
            ok, message = ai.check(provider)
            self.call_soon(lambda: status.configure(text=message,
                                                    fg=P.success if ok else P.error))
        threading.Thread(target=worker, daemon=True).start()

    def _save_permissions(self):
        for name, (enabled, confirm) in self._skill_vars.items():
            memory.set_permission(name, enabled.get(), confirm.get())
        self.skill_note.configure(text="Разрешения сохранены.", fg=P.success)

    def _save_routine(self):
        try:
            steps = json.loads(self.routine_steps.get("1.0", "end").strip() or "[]")
            if not isinstance(steps, list):
                raise ValueError("нужен список")
            for step in steps:
                if not isinstance(step, dict) or not skills.exists(step.get("skill", "")):
                    raise ValueError("неизвестный skill в шагах")
                skills.validate_params(step["skill"], step.get("params") or {})
            routine_id = memory.save_routine(self.routine_name.get(), steps,
                                             self.routine_trigger.get())
            if not routine_id:
                raise ValueError("задайте название")
            self.routine_note.configure(text="Сохранено.", fg=P.success)
            self._refresh_routines()
        except (ValueError, json.JSONDecodeError) as exc:
            self.routine_note.configure(text="Ошибка: %s" % exc, fg=P.error)

    def _refresh_routines(self):
        if not hasattr(self, "routine_list"):
            return
        for child in self.routine_list.winfo_children():
            child.destroy()
        routines = memory.list_routines()
        if not routines:
            tk.Label(self.routine_list, text="Рутин пока нет.", bg=P.bg_secondary,
                     fg=P.text_tertiary, padx=12, pady=12).pack(fill=tk.X)
            return
        for routine in routines:
            card = self._card(self.routine_list, routine["name"],
                              "%d шагов" % len(routine["steps"]))
            row = tk.Frame(card, bg=P.bg_secondary)
            row.pack(fill=tk.X, padx=12, pady=(0, 10))
            tk.Label(row, text=routine.get("trigger_phrase") or "без голосового триггера",
                     bg=P.bg_secondary, fg=P.text_tertiary,
                     font=theme.font("xs")).pack(side=tk.LEFT)
            self._button(row, "Запустить", lambda name=routine["name"]: self._run_routine(name)).pack(side=tk.RIGHT)

    def _run_routine(self, name):
        if self.runtime:
            self.runtime.submit_text("запусти режим " + name, self._external_window)
            self.show_chat()
        else:
            self.routine_note.configure(text="Запустите основной режим Jarvis.", fg=P.warning)

    def _add_note(self):
        text = self.note_input.get().strip()
        if text:
            memory.add_note(text)
            self.note_input.set("")
            self._refresh_memory()

    def _refresh_memory(self):
        if not hasattr(self, "memory_text"):
            return
        learned = memory.recent(8)
        notes = memory.list_notes(8)
        lines = ["ВЫУЧЕННЫЕ КОМАНДЫ  %d" % memory.count()]
        lines += ["• %s → %s" % (item["phrase"], item["skill"]) for item in learned]
        lines += ["", "ЗАМЕТКИ  %d" % len(notes)]
        lines += ["• %s" % item["text"] for item in notes]
        self.memory_text.configure(text="\n".join(lines))

    def _clear_learned(self):
        memory.clear()
        self._refresh_memory()

    def run_test(self, key):
        handler = self.on_test.get(key)
        if handler is None:
            self._set_diagnostic("Проверка недоступна.")
            return
        self._set_diagnostic("Проверяю…")

        if self.runtime:
            if not self.runtime.diagnostic(handler, lambda message: self.call_soon(
                    lambda: self._set_diagnostic(message))):
                self._set_diagnostic("Очередь заполнена. Нажмите «Стоп» и повторите.")
            return

        def worker():
            try:
                message = handler()
            except Exception as exc:
                message = "Ошибка: %s: %s" % (type(exc).__name__, exc)
            self.call_soon(lambda: self._set_diagnostic(message))
        threading.Thread(target=worker, daemon=True).start()

    def _set_diagnostic(self, text):
        if not hasattr(self, "check_output"):
            self.show_tab("diagnostics")
        self.check_output.configure(state="normal")
        self.check_output.delete("1.0", "end")
        self.check_output.insert("1.0", str(text))
        self.check_output.configure(state="disabled")

    def _refresh_home(self):
        if not hasattr(self, "home_state"):
            return
        snap = self.state.snapshot()
        status = snap.get("status", "idle")
        color = theme.STATE_COLORS.get(status, self.accent)
        self.home_state.configure(text=STATUS_LABELS.get(status, status.upper()), fg=color)
        provider = snap.get("provider") or (ai.chain()[0] if ai.chain() else "LOCAL ONLY")
        self.home_metrics["mic"].configure(text="ОШИБКА" if snap.get("mic_error") else
                                           "ПАУЗА" if snap.get("paused") else "ГОТОВ")
        self.home_metrics["provider"].configure(text=str(provider).upper())
        self.home_metrics["memory"].configure(text="%d КОМАНД" % memory.count())
        self.home_metrics["reminders"].configure(text="%d АКТИВНЫХ" % len(memory.pending_reminders()))
        actions = memory.recent_actions(6)
        self.home_actions.configure(text="Действий пока нет." if not actions else "\n".join(
            "%s  ·  %s" % (item["skill"], item["status"]) for item in actions))

    def toggle_pause(self):
        paused = not self.state.snapshot().get("paused", False)
        self.state.set(paused=paused, status="paused" if paused else "idle",
                       heard="", reply="")
        self.menu.entryconfigure(1, label="Продолжить слушать" if paused else "Пауза микрофона")
        if self.on_toggle_pause:
            self.on_toggle_pause(paused)

    def quit(self):
        self._closing = True
        if self.on_quit:
            self.on_quit()
        try:
            self.root.after(60, self.root.destroy)
        except tk.TclError:
            pass

    def _tick(self):
        if self._closing:
            return
        for _ in range(30):
            try:
                callback = self._callbacks.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception as exc:
                self.state.set(error="Ошибка интерфейса: %s" % exc)
        self._track_target()
        snap = self.state.snapshot()
        self._refresh_chat(snap)
        self._refresh_tasks(snap)
        busy = snap.get("status") in ("listening", "hearing", "thinking", "acting", "speaking", "asking")
        if busy or snap.get("version") != self._last_version or abs(self._width - self.PILL_MIN) > 1:
            self._last_version = snap.get("version")
            self._draw_pill(snap)
        status = snap.get("status", "idle")
        self.sidebar_state.configure(text="● " + STATUS_LABELS.get(status, status.upper()),
                                     fg=theme.STATE_COLORS.get(status, self.accent))
        self.header_status.configure(text=PILL_STATUS_LABELS.get(status, STATUS_LABELS.get(status, status.upper())))
        if self.expanded and self._tab == "home" and time.monotonic() - self._last_home_refresh > 1:
            self._last_home_refresh = time.monotonic()
            self._refresh_home()
        delay = theme.MOTION["active_tick"] if busy and not cfg.REDUCED_MOTION else 150
        self.root.after(delay, self._tick)

    def run(self):
        self.root.after(50, self._tick)
        self.root.mainloop()


def preview(seconds=18, open_center=True):
    import random

    state = UiState()
    island = Island(state)
    scenario = [
        ("idle", "", "", ""),
        ("listening", "", "", ""),
        ("hearing", "открой телеграм", "", ""),
        ("thinking", "открой телеграм", "", "Groq"),
        ("speaking", "", "Telegram открыт.", "Groq"),
        ("asking", "", "Точно закрыть программу?", "LOCAL"),
        ("dictating", "", "Режим ограничен активной сессией.", "LOCAL"),
        ("error", "", "Провайдер недоступен — использую локальный путь.", "LOCAL"),
        ("idle", "", "", ""),
    ]
    started = time.time()

    def step(index=0):
        if time.time() - started > seconds:
            island.quit()
            return
        status, heard, reply, provider = scenario[index % len(scenario)]
        state.set(status=status, heard=heard, reply=reply, provider=provider,
                  level=random.uniform(0.25, 0.85))
        island.root.after(1400, lambda: step(index + 1))

    if open_center:
        island.root.after(350, lambda: island.toggle_panel(True))
    island.root.after(200, step)
    island.run()

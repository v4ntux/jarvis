"""Jarvis 3 (server/jarvis) sitting the exam: its own brain, chat and group code with the real models, wired to
the pretend world instead of Telegram, the laptop and the server.

It needs the old bot's dependencies and an API key, so it runs on the server from a folder that holds both
packages (jarvis/ copied from the repo's server/jarvis, and jarvis4/), with /opt/jarvis/.env loaded.
It is wired the way jarvis/__main__.py wires the pieces: the owner's messages queue while a task runs, «стоп»
cancels the running task, a watcher says «🔔» and then runs its task. Whatever the old code would send to
Telegram, the laptop or the server lands in the world instead. Gemini is off: the owner's chat already went to
Claude first, and the exam must not burn the free Gemini quota.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
import tempfile
import time

from ..scenario import Event
from ..world import JARVIS, OWNER, Message, Offline, Person, World
from .base import Agent

KINDS_RU = {"sticker": "стикер", "gif": "гифка", "photo": "фото", "video": "видео", "voice": "голосовое"}


def label(m: Message) -> str:
    """How the old bot wrote a sticker or a GIF into the talk: «[стикер 😡]»."""
    if not m.media:
        return ""
    words = [KINDS_RU.get(m.media.get("kind"), "вложение"), m.media.get("emoji") or m.media.get("about") or ""]
    return "[" + " ".join(w for w in words if w) + "]"


class Jarvis3(Agent):
    name = "jarvis3"

    async def start(self, world: World, scenario) -> None:
        await super().start(world, scenario)
        self.tmp = tempfile.mkdtemp(prefix="exam-j3-")
        os.environ["JARVIS_DB"] = os.path.join(self.tmp, "jarvis.db")
        from jarvis import config, style  # imported here: only the server has them
        from jarvis.__main__ import BURST_QUIET, BUSY_NOTICE_AFTER, together
        from jarvis.chat import Chat
        from jarvis.front import Front
        from jarvis.gemini import Strangers
        from jarvis.groups import Groups
        from jarvis.memory import Memory

        self.config, self.style, self.together = config, style, together
        self.burst_quiet, self.busy_after = BURST_QUIET, BUSY_NOTICE_AFTER
        self.cfg = config.load()
        self.memory = Memory(self.cfg.db_path)
        gemini = _NoGemini()
        self.brain = _brain_class()(self.cfg, self.memory, WorldHub(world), world)
        self.brain.tg = WorldTelegram(world)
        self.front = Front(self.brain, gemini)
        chat = Chat(self.cfg, self.memory, gemini)
        self.strangers = Strangers(self.cfg, self.memory, chat)
        self.groups = Groups(self.cfg, self.memory, chat)
        self.task: asyncio.Task | None = None  # the brain's current request (what «стоп» cancels)
        self.started_at = 0.0
        self.waiting: list[str] = []
        self.feeder: asyncio.Task | None = None
        self.told = False
        self.background: set[asyncio.Task] = set()
        self._seed()

    def _seed(self) -> None:
        """What happened before the scenario, in the old bot's own memory format."""
        w, mem = self.world, self.memory
        done: dict[str, list[str]] = {}
        for a in w.past_actions:
            done.setdefault(a.get("after", ""), []).append(a["tool"])
        for m in w.history(OWNER):
            text = (f"{label(m)} {m.text}".strip())
            if m.sender == JARVIS:
                # The old brain kept only tool names after its answers: «[действия: …]»
                acts = done.get(m.key)
                mem.add("assistant", text + (f"\n[действия: {', '.join(acts)}]" if acts else ""))
            else:
                mem.add("user", text)
        for fact in w.facts:
            mem.remember(fact)
        for key, person in w.people.items():
            if key == OWNER:
                continue
            for m in w.history(key):
                mem.add_peer(person.id, person.name, "model" if m.sender == JARVIS else "user",
                             f"{label(m)} {m.text}".strip())
        for key, g in w.groups.items():
            mem.group_seen(g.id, g.title)
            for m in w.history(key):
                sender = w.people.get(m.sender)
                mem.group_add(g.id, m.id, sender.id if sender else 0, w.name_of(m.sender),
                              f"{label(m)} {m.text}".strip(), media=bool(m.media),
                              kind="reply" if m.sender == JARVIS else "")

    # ---- the exam's side ---------------------------------------------------------------------------------------

    async def deliver(self, index: int, event: Event, msg: Message | None) -> None:
        if event.kind == "laptop_online":
            self._spawn(self._laptop_online())
        elif event.kind == "new_session":
            self.memory.clear()  # what /reset did: the talk goes, the facts stay
        elif event.chat in self.world.groups:
            self._spawn(self._from_group(event, msg))
        elif event.sender == OWNER:
            await self._from_owner(msg)
        else:
            self._spawn(self._from_stranger(event, msg))

    def busy(self) -> bool:
        running = self.task is not None and not self.task.done()
        feeding = self.feeder is not None and not self.feeder.done()
        return running or feeding or bool(self.background) or bool(self.waiting)

    def cost(self) -> float:
        if getattr(self, "spent", None) is not None:  # counted before stop() closed the database
            return self.spent
        return self.memory.spent_since(0) if hasattr(self, "memory") else 0.0

    async def stop(self) -> None:
        tasks = [t for t in [getattr(self, "task", None), getattr(self, "feeder", None),
                             *getattr(self, "background", ())] if t and not t.done()]
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if hasattr(self, "memory"):
            self.spent = self.memory.spent_since(0)
            self.memory.db.close()
        shutil.rmtree(getattr(self, "tmp", ""), ignore_errors=True)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    # ---- the owner's chat, as __main__.handle_owner / feed / respond do it ------------------------------------

    async def _from_owner(self, msg: Message) -> None:
        from jarvis import intents
        text = f"{label(msg)} {msg.text}".strip()  # the old bot read raw_text only: what he answered is lost
        command = text.split(maxsplit=1)[0].lower() if text.startswith("/") else ""
        if command == "/stop":
            self._stop_task()
            return
        if command == "/reset":
            self.memory.clear()
            self.world.say(OWNER, "Историю разговора очистил. Факты из /memory остались.")
            return
        intent = intents.parse(text, dt.datetime.now(self.config.TZ))
        if intent is not None:
            if intent.kind == "stop":
                if self._stop_task():
                    return
            else:
                self._call_intent(text, intent)
                return
        self.waiting.append(text)
        if self.task and not self.task.done() and not self.told and time.time() - self.started_at > self.busy_after:
            self.told = True
            self.world.say(OWNER, "Секунду, доделываю прошлое, потом отвечу и на это. «стоп» — бросить прошлое.")
        if self.feeder is None or self.feeder.done():
            self.feeder = asyncio.create_task(self._feed())

    def _call_intent(self, text: str, intent) -> None:
        if intent.at is None or intent.at - time.time() < 5:
            self.world.call(OWNER, "say", "Ты просил позвонить.")
            answer = "Звоню."
        else:
            when = dt.datetime.fromtimestamp(intent.at, self.config.TZ)
            self.world.add_reminder("Ты просил позвонить.", f"{when:%H:%M}", call=True)
            answer = f"Окей, наберу в {when:%H:%M}."
        self.memory.add("user", text)
        self.memory.add("assistant", answer)
        self.world.say(OWNER, answer)

    def _stop_task(self) -> bool:
        if not self.task or self.task.done():
            return False
        self.task.cancel()
        return True

    async def _feed(self) -> None:
        while self.waiting:
            await asyncio.sleep(self.burst_quiet)
            while self.task and not self.task.done():
                await asyncio.sleep(0.2)
            batch = [{"text": t, "images": [], "spoken": False, "media": []} for t in self.waiting]
            del self.waiting[:]
            self.told = False
            await self._respond(self.together(batch)[0])

    async def _respond(self, text: str) -> None:
        from jarvis.brain import Session

        w = self.world
        session = Session(_noop, self._confirm, self._send_file)
        self.task = asyncio.create_task(self.front.reply(text, session, None, None))
        self.started_at = time.time()
        try:
            answer = await self.task
        except asyncio.CancelledError:
            answer = "Остановил."
        except Exception as e:
            answer = f"Ошибка мозга: {type(e).__name__}: {e}"[:500]
        finally:
            self.task = None
        marks = self.style.split_marks(answer)
        last = next((m.key for m in reversed(w.messages) if m.chat == OWNER and m.sender == OWNER), None)
        if marks.reaction and not marks.extras:
            w.react(OWNER, last, marks.reaction)
        plain = self.style.plain(marks.text)
        if plain:
            w.say(OWNER, plain)
        if marks.sticker:
            w.sticker(OWNER, marks.sticker)
        if marks.gif:
            w.gif(OWNER, marks.gif)

    async def _confirm(self, question: str) -> bool:
        return self.world.confirm(question)

    async def _send_file(self, name: str, data: bytes) -> None:
        self.world.effect("tg.file", to=OWNER, name=name, size=len(data))

    # ---- watchers: «🔔», then the task (__main__.on_event) --------------------------------------------------

    async def _laptop_online(self) -> None:
        for wt in [x for x in self.world.watchers if x.get("when") == "laptop_online"]:
            what = f"Ноутбук вышел на связь: {wt.get('then') or wt.get('notify', '')}"
            self.world.say(OWNER, "🔔 " + (wt.get("notify") or f"Сработало: {what}"))
            if wt.get("then"):
                while self.task and not self.task.done():
                    await asyncio.sleep(0.5)
                await self._respond(f"[Сработало автоматически: {what}]\n{wt['then']}")

    # ---- groups and strangers (tg_user._from_group / _from_stranger) ----------------------------------------

    async def _from_group(self, event: Event, msg: Message) -> None:
        w, g = self.world, self.world.groups[event.chat]
        person = w.people[event.sender]
        owner = event.sender == OWNER
        name = person.name
        text = msg.text
        tag = label(msg)
        self.memory.group_add(g.id, msg.id, person.id, name, f"{tag} {text}".strip(), media=bool(tag))
        replied_to_me = bool(msg.reply_to and w.message(msg.reply_to).sender == JARVIS)
        addressed = self.groups.addressed(text, False, replied_to_me)
        switch = self.groups.owner_switch(text) if addressed and owner else None
        if switch:
            self.memory.set_group_mode(g.id, switch)
            w.react(event.chat, msg.key, "🫡" if switch == "quiet" else "🔥")
            return
        spontaneous = not addressed
        if spontaneous and not (self.memory.group_mode(g.id) == "on"
                                and self.groups.spontaneous_wanted(bool(tag), text)):
            return
        if not self.groups.may_speak(g.id, spontaneous):
            return
        ask = self.groups.glance if spontaneous else self.groups.answer
        verdict = await ask(g.id, g.title, f"{name} (хозяин)" if owner else name, text or tag, "", [])
        if not verdict:
            return
        if verdict.reaction and not (verdict.sticker or verdict.gif):
            w.react(event.chat, msg.key, verdict.reaction)
        sent = w.say(event.chat, verdict.answer, reply_to=msg.key) if verdict.answer else None
        if verdict.sticker:
            w.sticker(event.chat, verdict.sticker)
        if verdict.gif:
            w.gif(event.chat, verdict.gif)
        self.groups.record(g.id, sent.id if sent else None,
                           verdict.answer or self.style.describe_marks(verdict) or "…", spontaneous)

    async def _from_stranger(self, event: Event, msg: Message) -> None:
        w = self.world
        person: Person = w.people[event.sender]
        text = f"{label(msg)} {msg.text}".strip()
        fresh = self.strangers.is_new(person.id)
        out = await self.strangers.reply(person.id, person.name, text, [])
        if out is None or out.spam:
            return
        if fresh:
            w.say(OWNER, f"👤 Мне впервые написал {person.name}:\n{text[:500]}")
        if out.reaction:
            w.react(event.chat, msg.key, out.reaction)
        if out.answer:
            w.say(event.chat, out.answer)
        if out.sticker:
            w.sticker(event.chat, out.sticker)
        if out.gif:
            w.gif(event.chat, out.gif)
        if out.note:
            w.say(OWNER, f"📨 {person.name} просит передать:\n{out.note}")


async def _noop(_line: str) -> None:
    pass


class _NoGemini:
    """Gemini switched off for the exam."""
    ready = False
    lite_first: list[str] = []
    models: list[str] = []

    async def text(self, *args, **kwargs) -> str:
        from jarvis.gemini import GeminiError
        raise GeminiError("в экзамене Gemini выключен")

    async def generate(self, *args, **kwargs):
        from jarvis.gemini import GeminiError
        raise GeminiError("в экзамене Gemini выключен")


class WorldTelegram:
    """What the brain uses of Jarvis's own Telegram account (tg_user.UserAccount)."""
    ready = True
    calls = True

    def __init__(self, world: World):
        self.world = world

    async def find_user(self, to: str):
        found = self.world.find(to)
        return found if isinstance(found, Person) else f"Ошибка: не нашёл «{to}» среди контактов и чатов."

    def display(self, user: Person) -> str:
        return f"{user.name} (@{user.username})" if user.username else user.name

    async def call_user(self, user: Person, text: str) -> bool:
        self.world.call(user.key, "say", text)  # the old account could only say the text and hang up
        return bool(self.world.effects[-1].data["answered"])

    async def call_owner(self, text: str) -> bool:
        self.world.call(OWNER, "say", text)
        return bool(self.world.effects[-1].data["answered"])

    async def send_to(self, user: Person, text: str, files=None) -> str:
        return self.world.send(user.key, text, [name for name, _ in files or []])

    async def set_profile(self, username: str = "", first_name: str = "", about: str = "") -> str:
        self.world.effect("tg.profile", username=username, first_name=first_name, about=about)
        return "Профиль обновлён."


class WorldHub:
    """The laptop as the brain sees it through the hub (hub.Hub)."""

    def __init__(self, world: World):
        self.world = world
        self.routines: list[str] = []

    @property
    def online(self) -> bool:
        return self.world.laptop_online

    async def call(self, tool: str, args: dict, timeout: float = 60) -> dict:
        from jarvis.hub import HandsOffline
        try:
            return {"text": self._run(tool, args or {})}
        except Offline as e:
            raise HandsOffline(str(e)) from e

    def _run(self, tool: str, a: dict) -> str:
        w = self.world
        if not w.laptop_online:
            raise Offline("ноутбук не подключён")
        simple = {
            "run_shell": lambda: w.laptop_shell(a.get("command", "")) or "(пусто)",
            "open_app": lambda: w.open_app(a.get("name", "")),
            "close_app": lambda: w.close(a.get("name", "")),
            "list_windows": w.windows_text,
            "focus_window": lambda: w.focus(a.get("title", "")),
            "open_url": lambda: w.open_url(a.get("url", "")),
            "type_text": lambda: w.type_text(a.get("text", "")),
            "media": lambda: w.media(a.get("action", "play_pause")),
            "set_volume": lambda: w.set_volume(int(a.get("percent", 40))),
            "clipboard_get": w.get_clipboard,
            "clipboard_set": lambda: w.set_clipboard(a.get("text", "")),
            "look": lambda: w.look() + "\nsnapshot_id: s1",
            "system_status": w.system_status,
            "list_routines": lambda: "Выученных приёмов нет.",
        }
        if tool in simple:
            return simple[tool]()
        if tool == "ui_action":
            params = a.get("params") or {}
            if a.get("action") == "click_element":
                return w.click(str(params.get("element_id", ""))) + "\n" + w.look() + "\nsnapshot_id: s2"
            if a.get("action") == "type_into_element":
                return w.type_text(params.get("text", "")) + "\n" + w.look() + "\nsnapshot_id: s2"
            w.effect("laptop.ui", action=a.get("action", ""), params=params)
            return "Сделал.\n" + w.look() + "\nsnapshot_id: s2"
        effects = {"hotkey": "Нажал.", "set_mute": "Готово.", "set_brightness": "Готово.", "send_file": "Отправил.",
                   "send_screen": "Скриншот отправлен.", "put_file": "Положил.", "write_file": "Записал.",
                   "add_app": "Запомнил.", "record_routine": "Записываю.", "stop_recording": "Записал приём.",
                   "run_routine": "Ошибка: такого приёма нет.", "forget_routine": "Удалил.",
                   "read_file": "Ошибка: файла нет."}
        if tool in effects:
            w.effect(f"laptop.{tool}", **{k: v for k, v in a.items() if isinstance(v, (str, int, bool))})
            return effects[tool]
        return f"Ошибка: {tool} в экзамене не поддержан."


def _brain_class():
    """The old Brain with its leaves (server shell, web, phone, files) pointing at the world."""
    from jarvis.brain import Brain

    class ExamBrain(Brain):
        def __init__(self, cfg, memory, hub, world: World):
            super().__init__(cfg, memory, hub)
            self.world = world

        async def _run_tool(self, name, args, session):
            self.world.note_tool(name, args)
            return await super()._run_tool(name, args, session)

        async def _send_server_file(self, path, session):
            self.world.effect("server.send_file", path=path)
            return {"text": f"Отправил {os.path.basename(path)}."}

        async def _server_tool(self, name, args):
            w = self.world
            if name == "web_search":
                return w.search(args.get("query", ""))
            if name in ("read_url", "browse"):
                return w.fetch(args.get("url", ""))
            if name == "browser_action":
                w.effect("server.browser", action=args.get("action", ""))
                return "Ошибка: страница не отвечает."
            if name == "server_shell":
                return w.server_shell(args.get("command", "")) or "(пусто)"
            if name == "server_job":
                w.server_shell(args.get("command", ""))
                return "Фоновая задача #1 запущена. Когда закончится, придёт сообщение."
            if name == "server_jobs":
                return "Фоновых задач нет."
            if name == "stop_job":
                return "Ошибка: такой задачи нет или она уже закончилась."
            if name in ("send_sms", "phone_call"):
                w.effect(f"phone.{name}", text=args.get("text", ""))
                return "Отправлено." if name == "send_sms" else "Звонок прошёл."
            if name == "monitor_status":
                return w.monitor()
            if name == "monitor_site":
                w.effect("monitor.site", url=args.get("url", ""), remove=bool(args.get("remove")))
                return "Готово."
            if name == "remember":
                w.remember(args.get("text", ""))
            elif name == "add_watcher":
                w.add_watcher({k: v for k, v in args.items() if isinstance(v, (str, int, bool))})
            elif name == "remove_watcher":
                w.remove_watcher(int(args.get("id", 0)))
            elif name == "set_reminder":
                w.add_reminder(args.get("text", ""), str(args.get("at") or args.get("in_minutes", "")),
                               bool(args.get("call")))
            return await super()._server_tool(name, args)

    return ExamBrain

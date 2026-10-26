"""Jarvis 4 sitting the exam: the real core and the real models, the world instead of Telegram, the laptop and the server.

Like the Jarvis 3 adapter it needs an Anthropic key, so the paid run happens on the server. The core is the same
object production uses; only the ports (Telegram, laptop, server, web) are the world's. Messages the world already
holds are seeded into a fresh store the way the real store would hold them.
"""
from __future__ import annotations

import os
import time

from ...config import Config
from ...core import Core, Incoming
from ...ports import CallResult, Offline, Platform, TGError, Window
from ...store import Store
from ...tools import telegram as tg_tools
from ..scenario import Event
from ..world import JARVIS, OWNER, OWNER_ID, World
from .base import Agent


class ExamStore(Store):
    """The real store, plus a word to the world whenever memory changes, so the exam can see it."""

    def __init__(self, world: World):
        super().__init__(":memory:")
        self.world = world

    def remember(self, text: str) -> int:
        self.world.effect("memory.remember", text=text)
        return super().remember(text)

    def add_lesson(self, text: str, trigger: str = "", source: str = "") -> int:
        self.world.effect("memory.lesson", text=text, when=trigger)
        return super().add_lesson(text, trigger, source)

    def add_watcher(self, spec: dict, due: float) -> int:
        self.world.effect("watcher.add", **{k: v for k, v in spec.items() if isinstance(v, (str, int, bool))})
        return super().add_watcher(spec, due)

    def remove_watcher(self, watcher_id: int) -> bool:
        self.world.effect("watcher.remove", id=watcher_id)
        return super().remove_watcher(watcher_id)

    def add_reminder(self, due: float, text: str, call: bool = False) -> int:
        self.world.effect("reminder.add", text=text, call=call)
        return super().add_reminder(due, text, call)


class WorldOut:
    """How the core talks in a chat: through the world, where the exam reads it."""

    def __init__(self, world: World, chat: str):
        self.world, self.chat = world, chat

    async def say(self, text: str, reply_ref: str = "") -> str:
        return self.world.say(self.chat, text, reply_ref or None).key

    async def ask(self, question: str) -> bool:
        return self.world.confirm(question)

    async def progress(self, line: str) -> None:
        self.world.timeline.append(f"  [ход] {line}")

    async def send_file(self, name: str, data: bytes) -> None:
        self.world.effect("tg.file", to=self.chat, name=name, size=len(data))

    async def typing(self) -> None:
        pass

    async def finish(self) -> None:
        pass


class WorldTelegram:
    def __init__(self, world: World):
        self.w = world

    def _last(self) -> str:
        return self.w.messages[-1].key

    async def lookup(self, query: str):
        return f"Ошибка: «{query}» не нашёл."

    async def sync_history(self, chat: str, limit: int) -> None:
        pass

    async def send(self, chat: str, text: str, files=None, reply_ref: str = "") -> str:
        said = self.w.send(chat, text, [n for n, _ in files or []])
        if said.startswith("Ошибка"):
            raise TGError(said)
        return self._last()

    async def forward(self, refs: list[str], to: str) -> list[str]:
        new: list[str] = []
        by_chat: dict[str, list[str]] = {}
        for ref in refs:
            by_chat.setdefault(self.w.message(ref).chat, []).append(ref)
        for source, group in by_chat.items():
            said = self.w.forward(source, group, to)
            if said.startswith("Ошибка"):
                raise TGError(said)
            new += [m.key for m in self.w.messages[-len(group):]]
        return new

    async def react(self, ref: str, emoji: str) -> None:
        self.w.react(self.w.message(ref).chat, ref, emoji)

    async def sticker(self, chat: str, emoji: str, reply_ref: str = "") -> str:
        self.w.sticker(chat, emoji)
        return self._last()

    async def gif(self, chat: str, query: str, reply_ref: str = "") -> str:
        self.w.gif(chat, query)
        return self._last()

    async def leave(self, chat: str) -> str:
        said = self.w.leave(chat)
        if said.startswith("Ошибка"):
            raise TGError(said)
        return said

    async def call(self, person: str, mode: str, goal: str) -> CallResult:
        said = self.w.call(person, mode, goal)
        answered = bool(self.w.effects[-1].data["answered"])
        return CallResult(answered, said)

    async def typing(self, chat: str) -> None:
        pass


class WorldLaptop:
    def __init__(self, world: World):
        self.w = world

    @property
    def online(self) -> bool:
        return self.w.laptop_online

    async def shell(self, command: str, timeout: int = 60) -> str:
        return self.w.laptop_shell(command)

    async def windows(self) -> list[Window]:
        self.w._need_laptop()
        self.w._seen("; ".join(f"{x.title} ({x.kind}, {x.process})" for x in self.w.windows) or "окон нет")
        return [Window(x.title, x.process, x.kind) for x in self.w.windows]

    async def look(self) -> str:
        return self.w.look()

    async def click(self, element: str) -> str:
        return self.w.click(element)

    async def open(self, name: str) -> str:
        return self.w.open_app(name)

    async def close(self, title: str) -> str:
        return self.w.close(title)

    async def focus(self, title: str) -> str:
        return self.w.focus(title)

    async def type(self, text: str) -> str:
        return self.w.type_text(text)

    async def keys(self, keys: str) -> str:
        self.w._need_laptop()
        self.w.effect("laptop.keys", keys=keys)
        return "Нажал."

    async def media(self, action: str) -> str:
        return self.w.media(action)

    async def volume(self, percent: int) -> str:
        return self.w.set_volume(percent)

    async def clipboard(self, text: str | None = None) -> str:
        return self.w.get_clipboard() if text is None else self.w.set_clipboard(text)

    async def screenshot(self) -> bytes:
        raise Offline("в экзамене у мира нет пикселей")


class WorldServer:
    def __init__(self, world: World):
        self.w = world

    async def shell(self, command: str, timeout: int = 60) -> str:
        return self.w.server_shell(command)

    async def monitor(self) -> str:
        return self.w.monitor()


class WorldWeb:
    def __init__(self, world: World):
        self.w = world

    async def search(self, query: str, news: bool = False) -> str:
        return self.w.search(query)

    async def fetch(self, url: str) -> str:
        return self.w.fetch(url)


class Jarvis4(Agent):
    name = "jarvis4"

    def __init__(self, llm_factory=None):
        self.llm_factory = llm_factory  # tests give a scripted model; the exam uses the real one

    async def start(self, world: World, scenario) -> None:
        await super().start(world, scenario)
        self.store = ExamStore(world)
        cfg = Config(owner_id=OWNER_ID, anthropic_key=os.environ.get("ANTHROPIC_API_KEY", ""), db_path=":memory:",
                     burst_quiet=0.05, burst_max=0.4)
        platform = Platform(tg=WorldTelegram(world), server=WorldServer(world), web=WorldWeb(world),
                            laptop=WorldLaptop(world))
        llm = self.llm_factory(self.store) if self.llm_factory else None
        if os.environ.get("EXAM_DEBUG"):
            llm = _Traced(llm or Core(cfg, self.store, platform).llm, world)
        self.core = Core(cfg, self.store, platform, llm=llm, outs=lambda chat: WorldOut(world, chat))
        run_tool = self.core.run_tool

        async def noted(tool, call, itx, show=False):  # the runner counts tool calls to time mid-task messages
            world.note_tool(call.name, call.input if isinstance(call.input, dict) else {})
            return await run_tool(tool, call, itx, show)

        self.core.run_tool = noted
        stop_all = self.core.stop_all

        async def stopped(chat="owner"):
            did = await stop_all(chat)
            if did:
                world.effect("stop", chat=chat)  # the judge reads «стоп» as a deed the world saw
            return did

        self.core.stop_all = stopped
        self._seed()

    def _seed(self) -> None:
        w, st = self.world, self.store
        base = time.time() - 3600
        for key, p in w.people.items():
            if key != OWNER:
                st.upsert_person(key, p.name, p.username, p.id, p.aliases, p.dialog, p.blocked_me)
        for key, g in w.groups.items():
            st.chat_seen(key, "group", g.title, g.id)
        for i, m in enumerate(w.messages):
            st.add_msg(m.key, m.chat, m.sender, m.text, m.media, m.reply_to or "", m.forward_of or "", m.relay,
                       ts=base + i)
        for a in w.past_actions:
            task = f"past:{a.get('after', '')}"
            st.journal_add(task, a["tool"], a.get("args") or {}, True, a.get("result", ""))
            if a.get("after"):
                st.db.execute("UPDATE msgs SET task = ? WHERE ref = ?", (task, a["after"]))
        st.db.commit()
        for fact in w.facts:
            st.remember(fact)

    async def deliver(self, index: int, event: Event, msg) -> None:
        core, w = self.core, self.world
        if event.kind == "laptop_online":
            batch = [({"source": x["when"], "what": x.get("notify", ""), "notify": x.get("notify", ""),
                       "task": x.get("then", ""), "auto": bool(x.get("auto")), "by": x.get("by", "")}, "")
                     for x in w.watchers if x.get("when") == "laptop_online"]
            if batch:
                core.spawn(core.on_watchers(batch))
        elif event.kind == "new_session":
            self.store.fresh_start("owner")
        else:
            group = event.chat in w.groups
            images = []
            await core.on_message(Incoming(
                ref=msg.key, chat=event.chat, sender=event.sender, text=event.text, media=event.media,
                reply_ref=event.reply_to or "", group=group, images=images))

    def busy(self) -> bool:
        return self.core.busy()

    def cost(self) -> float:
        return getattr(self, "spent", None) if getattr(self, "spent", None) is not None else self.store.spent_since(0)

    async def stop(self) -> None:
        tasks = [t for t in [self.core.current, self.core.feeder, *self.core.background] if t and not t.done()]
        for t in tasks:
            t.cancel()
        import asyncio
        await asyncio.gather(*tasks, return_exceptions=True)
        self.spent = self.store.spent_since(0)
        self.store.close()


class _Traced:
    """EXAM_DEBUG=1: every request to the model goes into the run's timeline, to see what the model was shown."""

    def __init__(self, inner, world: World):
        self.inner, self.world = inner, world

    @property
    def ready(self) -> bool:
        return True

    async def complete(self, model, system, messages, tools=None, **kw):
        steady = system if isinstance(system, str) else system[0]["text"]
        live = "" if isinstance(system, str) or len(system) < 2 else system[1]["text"]
        last = messages[-1]["content"] if messages else ""
        self.world.timeline.append(f"  [запрос {model}] live={live[:400]!r} последнее={str(last)[:700]!r} "
                                   f"инструменты={[t['name'] for t in tools or []]}")
        reply = await self.inner.complete(model, system, messages, tools, **kw)
        self.world.timeline.append(f"  [ответ] текст={reply.text[:300]!r} вызовы={[(c.name, c.input) for c in reply.calls]}")
        return reply

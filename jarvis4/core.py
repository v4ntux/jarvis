"""The core: messages come in, interactions go through the mouth and the brain, answers go out.

It owns the rules that must hold whatever the model says: «стоп» is instant and also clears the queue, every tool
passes the guard and lands in the journal, the owner's messages that arrive together are one request, an answer
that claims more than the journal shows gets rewritten, and strangers never see the owner's world.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable

from . import clock, context, persona, verify, watch
from .agent import Agent, Outcome
from .budget import Budget, Tier
from .config import Config
from .guard import Verdict
from .llm import LLM
from .ports import Platform
from .store import Msg, Store
from .tools import Ctx, Risk, Tool, all_tools
from .types import Interaction, Mode, Out, Result, fail

log = logging.getLogger("jarvis4.core")

STOP = re.compile(r"^\W*(стоп|stop|стой|хватит|остановись|остановите|отбой|отмена|прекрати|stoppp*)(\W+(нах\w*|уже|блять|"
                  r"бля|пожалуйста|все|всё|зоебал\w*|заебал\w*|ну))*\W*$", re.I)
YES = re.compile(r"^\W*(да|ага|давай|делай|выполняй|ок|окей|ok|okay|yes|da|davay|конечно|можно)\W*$", re.I)
NO = re.compile(r"^\W*(нет|не надо|не нужно|отмена|no|net|не делай)\W*$", re.I)
NAME = re.compile(r"(?<!\w)(?:джарвис|жарвис|джарви|джарвик|jarvis|zharvis|dzharvis|jarvi)\w*", re.I)
CONFIRM_SECONDS = 300


@dataclass
class Incoming:
    ref: str
    chat: str  # the chat key: a person's key for a private chat, a group's key for a group
    sender: str
    text: str = ""
    media: dict | None = None
    reply_ref: str = ""
    forward_ref: str = ""
    images: list[str] = field(default_factory=list)  # base64 pictures Claude can look at
    group: bool = False
    mentioned: bool = False  # the message names Jarvis or answers him
    ts: float = field(default_factory=time.time)


class Core:
    TIER_CHECK = Tier.MOUTH

    def __init__(self, cfg: Config, store: Store, platform: Platform, llm: LLM | None = None,
                 outs: Callable[[str], Out] | None = None):
        self.cfg, self.store, self.platform = cfg, store, platform
        self.llm = llm or LLM(cfg, store)
        self.budget = Budget(cfg, store)
        self.registry: dict[str, Tool] = all_tools()
        self.READ_TOOLS = {t.name for t in self.registry.values() if t.risk == Risk.READ}
        self.mouth, self.brain = Agent(self, "mouth"), Agent(self, "brain")
        self.outs = outs  # chat key → how to talk in that chat (the transport sets it)
        self.frozen_until = 0.0
        self.queue: list[Incoming] = []
        self.current: asyncio.Task | None = None  # the owner's request being worked on: what «стоп» cancels
        self.feeder: asyncio.Task | None = None
        self.background: set[asyncio.Task] = set()
        self.answerers: list[Callable[[str], bool]] = []  # a pending yes/no question takes the owner's next word
        self.owner_hooks: list[Callable[[], None]] = []  # any word from the owner in his chat (stops a running alarm)
        self.commands = None  # async (text) → answer | None: the owner's /commands, set by the app
        self.group_last_spoke: dict[str, tuple[float, str]] = {}  # group → (when Jarvis last spoke, to whom)
        store.upsert_person("owner", cfg.owner_name, aliases=["ван", "van", "батя", "хозяин"], dialog=True)

    # ---- small helpers -----------------------------------------------------------------------------------------

    def name_of(self, key: str) -> str:
        if key == "jarvis":
            return "Джарвис"
        if key == "owner":
            return self.cfg.owner_name
        person = self.store.person(key)
        if person:
            return person.name
        chat = self.store.chat(key)
        return chat[1] if chat else key

    def frozen(self) -> bool:
        return time.time() < self.frozen_until

    def busy(self) -> bool:
        running = self.current is not None and not self.current.done()
        feeding = self.feeder is not None and not self.feeder.done()
        return running or feeding or bool(self.queue) or bool(self.background)

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    def out_for(self, chat: str) -> Out:
        if self.outs is None:
            raise RuntimeError("транспорт не подключён (core.outs)")
        return self.outs(chat)

    # ---- incoming ----------------------------------------------------------------------------------------------

    def ingest(self, inc: Incoming) -> Msg:
        if not inc.group and inc.sender != "owner":
            self.store.upsert_person(inc.sender, dialog=True)
        return self.store.add_msg(inc.ref, inc.chat, inc.sender, inc.text, inc.media, inc.reply_ref, inc.forward_ref,
                                  ts=inc.ts)

    async def on_message(self, inc: Incoming) -> None:
        """Every message Jarvis's account receives. Returns at once; the work goes on in the background."""
        self.ingest(inc)
        if inc.sender == "owner" and not inc.group:
            for hook in self.owner_hooks:
                hook()
            await self._from_owner(inc)
        elif inc.group:
            self.spawn(self._from_group(inc))
        else:
            self.spawn(self._from_person(inc))

    async def _from_owner(self, inc: Incoming) -> None:
        text = inc.text or ""
        if any(answer(text) for answer in self.answerers):
            return  # the answer to a yes/no question
        if self.commands is not None and text.startswith("/"):
            answered = await self.commands(text)
            if answered is not None:
                if answered:
                    await self.out_for(inc.chat).say(answered)
                return
        if STOP.match(text) and len(text) < 40:
            if await self.stop_all(inc.chat):
                return
        self.queue.append(inc)
        if self.feeder is None or self.feeder.done():
            self.feeder = asyncio.create_task(self._feed())

    async def stop_all(self, chat: str = "owner") -> bool:
        """«Стоп»: the running task, the queue, pending questions, watchers and timers (for a while). True when said."""
        was_busy = self.current is not None and not self.current.done()
        queued = bool(self.queue)
        self.queue.clear()
        self.frozen_until = time.time() + self.cfg.freeze_seconds
        if self.current and not self.current.done():
            self.current.cancel()
        for answer in self.answerers:
            answer("нет")
        if was_busy or queued:
            await self.out_for(chat).say("стоп, всё бросил")
            return True
        self.frozen_until = time.time() + self.cfg.freeze_seconds
        return False

    # ---- the owner's lane --------------------------------------------------------------------------------------

    async def _feed(self) -> None:
        while self.queue:
            started, seen = time.time(), -1
            while len(self.queue) != seen and time.time() - started < self.cfg.burst_max:  # until the burst stops
                seen = len(self.queue)
                await asyncio.sleep(self.cfg.burst_quiet)
            while self.current is not None and not self.current.done():  # the previous request first
                await asyncio.sleep(0.2)
            batch, self.queue = self.queue, []
            if not batch:
                continue
            self.current = asyncio.create_task(self._run_owner(batch))
            try:
                await self.current
            except asyncio.CancelledError:
                if self.current is None or not self.current.cancelled():
                    raise  # the feeder itself was cancelled
            except Exception:
                log.exception("owner request failed")
            finally:
                self.current = None

    def _interaction(self, batch: list[Incoming], kind: str, owner: bool, spontaneous: bool = False) -> Interaction:
        chat, sender = batch[-1].chat, batch[-1].sender
        msgs = [m for m in (self.store.msg(i.ref) for i in batch) if m]
        text = "\n".join(i.text or context.media_label(self.store.msg(i.ref)) for i in batch
                         if i.text or i.media).strip()
        itx = Interaction(kind=kind, chat=chat, sender=sender, text=text, msgs=msgs, owner=owner,
                          out=self.out_for(chat), spontaneous=spontaneous,
                          images=[b for i in batch for b in i.images][:6])
        return itx

    async def _run_owner(self, batch: list[Incoming]) -> None:
        itx = self._interaction(batch, "owner", True)
        await self.interact(itx, batch[-1].reply_ref)

    async def interact(self, itx: Interaction, reply_ref: str = "") -> None:
        """One request from the owner: the mouth answers or hands over, the brain works, the answer is checked."""
        replied = self.store.msg(reply_ref) if reply_ref else None
        itx.mode = persona.detect_mode(itx.text, bool(replied and replied.sender == "jarvis"))
        itx.think = persona.wants_thinking(itx.text)
        if persona.is_correction(itx.text):
            itx.note = ("Ван тебя поправляет или недоволен. Исправь то, что не так, и запиши урок через learn_lesson "
                        "(что делать и чего не делать), чтобы это не повторилось.")
        self.store.task_start(itx.id, itx.chat, itx.kind, itx.text)
        status = "done"
        answer = ""
        try:
            await itx.out.typing()
            outcome = await self.mouth.run(itx)
            if not outcome.handoff and not outcome.silent and self._lied(itx, outcome):
                outcome.handoff = f"normal:{itx.text}"  # it said it did something it never did: the brain really does it
            if outcome.handoff:
                effort, _, goal = outcome.handoff.partition(":")
                if itx.mode == Mode.CHAT:
                    itx.mode = Mode.WORK
                outcome = await self.brain.run(itx, goal, effort)
            answer = outcome.text
            if not outcome.silent:
                answer = await verify.check(self, itx, itx.text, answer)
                await self.deliver(itx, answer)
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception as e:
            status = "error"
            log.exception("interaction failed")
            await itx.out.say(f"споткнулся: {type(e).__name__}. гляну логи, если надо — скажи")
        finally:
            self.store.task_end(itx.id, status, itx.spent, answer)
            await self._finish(itx)

    async def _finish(self, itx: Interaction) -> None:
        try:
            await itx.out.finish()
        except Exception:
            log.debug("finish failed", exc_info=True)

    def _lied(self, itx: Interaction, outcome: Outcome) -> bool:
        """The mouth called no tool, yet its answer claims a deed: a task it only pretended to do."""
        return (outcome.tools_used == 0 and itx.mode != Mode.CHAT and bool(persona.TASK.search(itx.text))
                and bool(persona.CLAIM.search(outcome.text)))

    async def deliver(self, itx: Interaction, text: str) -> None:
        text = persona.finalize(text)
        if not text:
            return
        for piece in persona.chunks(text):
            ref = await itx.out.say(piece, itx.ref if itx.kind == "group" else "")
            if ref:
                self.store.add_msg(ref, itx.chat, "jarvis", piece, reply_ref=itx.ref if itx.kind == "group" else "",
                                   task=itx.id)
        if itx.kind == "group":
            self.group_last_spoke[itx.chat] = (time.time(), itx.sender)

    # ---- strangers and groups ----------------------------------------------------------------------------------

    async def _from_person(self, inc: Incoming) -> None:
        person = self.store.person(inc.sender)
        if person and person.blocked:
            return
        itx = self._interaction([inc], "person", False)
        itx.mode = Mode.CHAT
        await self._others(itx)

    async def _from_group(self, inc: Incoming) -> None:
        mode = (self.store.chat(inc.chat) or ("", "", "on"))[2]
        owner = inc.sender == "owner"
        if mode == "off":
            return
        addressed = self.called(inc) or self._in_conversation(inc)
        if mode == "quiet" and not addressed:
            return
        if not addressed and not self.group_wants_to_speak(inc):
            return
        itx = self._interaction([inc], "group", owner, spontaneous=not addressed)
        itx.mode = persona.detect_mode(itx.text) if owner else Mode.CHAT
        if owner and itx.mode == Mode.WORK:
            itx.mode = Mode.WORK
        if owner:
            await self.interact(itx, inc.reply_ref)
        else:
            await self._others(itx)

    def called(self, inc: Incoming) -> bool:
        """Named, @mentioned, or answering one of Jarvis's own messages."""
        if inc.mentioned or NAME.search(inc.text or ""):
            return True
        target = self.store.msg(inc.reply_ref) if inc.reply_ref else None
        return bool(target and target.sender == "jarvis")

    def _in_conversation(self, inc: Incoming) -> bool:
        """Jarvis spoke to this person a moment ago: their next words are for him, no name needed."""
        when, to = self.group_last_spoke.get(inc.chat, (0.0, ""))
        return time.time() - when < 120 and (to == inc.sender or inc.sender == "owner")

    def group_wants_to_speak(self, inc: Incoming) -> bool:
        return False  # stage 4: spontaneous remarks (caps, taste); until then he speaks only when called

    async def _others(self, itx: Interaction) -> None:
        """Someone who is not the owner: the mouth with the few harmless tools and no view of the owner's world."""
        self.store.task_start(itx.id, itx.chat, itx.kind, itx.text)
        answer, status = "", "done"
        try:
            await itx.out.typing()
            outcome = await self.mouth.run(itx)
            answer = outcome.text
            if not outcome.silent and answer.strip():
                await self.deliver(itx, answer)
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception:
            status = "error"
            log.exception("chat with %s failed", itx.sender)
        finally:
            self.store.task_end(itx.id, status, itx.spent, answer)

    async def relay(self, itx: Interaction, note: str) -> None:
        """A stranger wants something passed on: the owner gets a note that stands for the messages behind it."""
        person = self.store.person(itx.sender)
        label = person.label if person else itx.sender
        source = [m.ref for m in itx.msgs]
        recent = [m for m in self.store.recent(itx.chat, 8) if m.sender != "jarvis" and m.media and m.ref not in source]
        source = [m.ref for m in recent][-4:] + source
        text = f"📨 {label} просит передать:\n{note}"
        ref = await self.platform.tg.send("owner", text)
        self.store.add_msg(ref, "owner", "jarvis", text, relay={"chat": itx.chat, "msgs": source})

    # ---- tools -------------------------------------------------------------------------------------------------

    async def run_tool(self, tool: Tool | None, call, itx: Interaction, show: bool = False) -> Result:
        """Guard → run → journal → progress line. The model sees only the result text."""
        if tool is None:
            result = fail(f"инструмента {call.name} нет.")
            self.store.journal_add(itx.id, call.name, call.input, False, result.text)
            return result
        ctx = Ctx(self, itx)
        args = call.input if isinstance(call.input, dict) else {}
        try:
            verdict = await tool.guard(ctx, args) if tool.guard else None
            verdict = verdict or Verdict("allow")
            if verdict.kind == "deny":
                result = Result(False, "Отказ охраны: " + verdict.text)
            elif verdict.kind == "ask" and verdict.scope not in itx.approved:
                if await itx.out.ask(verdict.text):
                    itx.approved.add(verdict.scope)
                    result = await tool.run(ctx, args)
                else:
                    itx.declined = True
                    result = Result(False, "Ван отказал. Не делай этого и не ищи обходной путь.")
            else:
                result = await tool.run(ctx, args)
        except asyncio.CancelledError:
            self.store.journal_add(itx.id, tool.name, args, False, "прервано стопом")
            raise
        except Exception as e:
            log.exception("tool %s failed", tool.name)
            result = fail(f"{type(e).__name__}: {e}")
        self.store.journal_add(itx.id, tool.name, args, result.ok, result.text, result.evidence)
        if show and tool.risk != Risk.READ or (show and not result.ok):
            await itx.out.progress(("✓ " if result.ok else "✗ ") + tool.line(args, result))
        return result

    # ---- watchers ----------------------------------------------------------------------------------------------

    async def on_watchers(self, batch: list[tuple[dict, str]]) -> None:
        """Watchers fired together: tell the owner once. A task only rides along as a question, unless the owner
        ordered it (auto): then it is announced and waits 30 seconds for a «стоп»."""
        if self.frozen():
            return
        heads, proposals, autos = [], [], []
        for spec, detail in batch:
            head = spec.get("notify") or f"Сработало: {spec['what']}"
            heads.append(head + (f": {detail[:300]}" if detail else ""))
            if spec.get("task"):
                (autos if spec.get("auto") else proposals).append(spec["task"])
        heads = list(dict.fromkeys(heads))
        text = "🔔 " + "; ".join(heads)
        if proposals:
            listing = "; ".join(f"{i}) {t}" for i, t in enumerate(proposals, 1)) if len(proposals) > 1 else proposals[0]
            text += f"\nвисело: {listing}. делать?"
        out = self.out_for("owner")
        ref = await out.say(text)
        if ref:
            self.store.add_msg(ref, "owner", "jarvis", text)
        if autos:
            plan = "; ".join(autos)
            ref = await out.say(f"через 30 с сделаю: {plan}. «стоп» — отменить")
            if ref:
                self.store.add_msg(ref, "owner", "jarvis", f"через 30 с сделаю: {plan}")
            self.spawn(self._after_notice(plan))

    async def _after_notice(self, plan: str) -> None:
        await asyncio.sleep(30)
        if self.frozen():
            return
        await self.run_own_task(plan, "Сработал наблюдатель, который Ван сам заказал. Выполни его задачу.")

    async def run_own_task(self, plan: str, note: str) -> None:
        """A task nobody typed: a watcher the owner ordered, what a finished job was to be followed by."""
        if self.frozen():
            return
        while self.current is not None and not self.current.done():  # the owner's own request goes first
            await asyncio.sleep(1)
        itx = Interaction(kind="owner", chat="owner", sender="owner", text=plan, msgs=[], owner=True,
                          out=self.out_for("owner"), mode=Mode.WORK)
        itx.note = note
        self.store.task_start(itx.id, "owner", "event", plan)
        status, answer = "done", ""
        self.current = asyncio.current_task()  # «стоп» reaches it like any other work
        try:
            outcome = await self.brain.run(itx, plan, "normal")
            answer = await verify.check(self, itx, plan, outcome.text)
            await self.deliver(itx, answer)
        except asyncio.CancelledError:
            status = "stopped"
            raise
        except Exception:
            status = "error"
            log.exception("own task failed")
        finally:
            self.store.task_end(itx.id, status, itx.spent, answer)
            await self._finish(itx)
            if self.current is asyncio.current_task():
                self.current = None

    async def on_job_done(self, what: str, job_id: int, code: int, tail: str, then: str = "") -> None:
        """A background job on the server ended: the owner hears it, and the follow-up he ordered starts."""
        if self.frozen():
            return
        state = "закончилась" if code == 0 else f"упала (код {code})"
        out = self.out_for("owner")
        text = f"фоновая задача #{job_id} «{what}» {state}" + ("\n" + tail[-600:] if code else "")
        ref = await out.say(text)
        if ref:
            self.store.add_msg(ref, "owner", "jarvis", text)
        if then and code == 0:
            goal = then + ("\n(данные: " + tail[-1500:] + ")" if tail else "")
            self.spawn(self.run_own_task(goal, "Фоновая задача закончилась, Ван заказал это продолжение. Выполни."))

    # ---- the yes/no question to the owner ----------------------------------------------------------------------

    def asker(self, say: Callable[[str], "asyncio.Future"]) -> Callable[[str], "asyncio.Future"]:
        """Builds a question function for a transport: the owner's next «да»/«нет» answers it."""
        pending: dict[str, asyncio.Future | None] = {"fut": None}

        def answer(text: str) -> bool:
            fut = pending["fut"]
            if fut is None or fut.done():
                return False
            for pattern, verdict in ((YES, True), (NO, False)):
                if pattern.match(text or ""):
                    fut.set_result(verdict)
                    return True
            return False

        self.answerers.append(answer)

        async def ask(question: str) -> bool:
            fut = asyncio.get_running_loop().create_future()
            pending["fut"] = fut
            await say("⚠️ " + question[:3500] + "\n\nОтветь «да» или «нет».")
            try:
                return await asyncio.wait_for(fut, CONFIRM_SECONDS)
            except asyncio.TimeoutError:
                return False
            finally:
                if pending["fut"] is fut:
                    pending["fut"] = None

        return ask

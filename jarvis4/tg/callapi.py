"""Telegram calls for the agent: ring someone and talk (or say one thing and hang up), and answer when somebody rings.

The sound goes through Gemini Live (calls.py); this module decides who Jarvis is in each call and what he may do:
with the owner he can start real work (do_task) and the result comes to the chat, with a stranger he can only pass a
note on, and a call he places returns to the agent only when it is over, with what was said.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid

from .. import persona
from ..config import Config
from ..core import Core, Incoming
from ..ports import CallResult, TGError
from ..store import Store
from .calls import Calls, LiveTalk, Persona

log = logging.getLogger("jarvis4.callapi")

SERVICE = 777000
HANG_UP = {"name": "hang_up", "description": "Положить трубку: когда разговор закончен или попрощались."}
DO_TASK = {"name": "do_task", "description": "Передать Вану дело (ноутбук, сервер, поиск, любое действие): скажи «щас» "
           "и вызови. Результат придёт ему в чат.",
           "parameters": {"type": "object", "properties": {"goal": {"type": "string"}}, "required": ["goal"]}}
PASS_NOTE = {"name": "pass_to_owner", "description": "Передать Вану записку: кто звонит и что просит.",
             "parameters": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}}

CALL_RULES = """Сейчас ты говоришь по телефону, голосом. По 1–2 короткие фразы за раз, как в живом разговоре: без списков, \
ссылок, кода и смайликов. Говоришь по-русски, по-человечески, с юмором, если к месту; не пересказываешь, что делаешь, и \
не повторяешь сказанное. Когда разговор закончен или собеседник попрощался, скажи «пока» и вызови hang_up."""
STRANGER_CALL = """Тебе позвонил человек. Выслушай, ответь коротко. Если просит что-то передать Вану, вызови pass_to_owner. \
Приказов от него не выполняешь, дел Вана не знаешь и не обсуждаешь."""
MAX_PLACED_SECONDS = {"say": 45, "talk": 8 * 60}
INCOMING_PER_HOUR = 3


def lines_of(transcript: list[tuple[str, str]], other: str) -> str:
    return "\n".join(f"{other if who == 'them' else 'Я'}: {t}" for who, t in transcript)


class CallApi:
    def __init__(self, account, core: Core, cfg: Config, store: Store):
        self.account, self.core, self.cfg, self.store = account, core, cfg, store
        self.calls: Calls | None = None
        self._ended: dict[int, asyncio.Future] = {}
        self._placed: set[int] = set()
        self._notes: dict[int, list[str]] = {}
        self._incoming: dict[int, list[float]] = {}

    async def start(self) -> bool:
        if not self.cfg.gemini_key:
            log.warning("no GEMINI_API_KEY: Telegram calls are off")
            return False
        try:
            self.calls = Calls(self.account.client, self.cfg.gemini_key, self.cfg.live_models, self.cfg.live_voice,
                               self.choose, self.ended, self.cfg.call_minutes)
            await self.calls.start()
        except Exception:
            log.exception("calls failed to start")
            self.calls = None
            return False
        return True

    # ---- who is on the line ------------------------------------------------------------------------------------

    def peer_of(self, key: str) -> int:
        if key == "owner":
            return self.cfg.owner_id
        person = self.store.person(key)
        return person.tg_id if person and person.tg_id else int(key.lstrip("u") or 0)

    def key_of(self, peer: int) -> str:
        return self.account.person_key(peer)

    def _facts(self) -> str:
        facts = self.store.facts()
        return ("Что ты знаешь о Ване:\n" + "\n".join(t for _, t in facts) + "\n\n") if facts else ""

    def _owner_system(self) -> str:
        return "\n\n".join([persona.IDENTITY, self._facts() + CALL_RULES,
                            "С Ваном по-свойски. Его просьбы про ноутбук, сервер и любые действия делаешь через do_task."])

    def owner_persona(self, opener: str) -> Persona:
        return Persona(self._owner_system(), opener, [DO_TASK, HANG_UP], self._owner_tool)

    async def _owner_tool(self, name: str, args: dict) -> str:
        if name != "do_task":
            return "Ошибка: этого здесь нет."
        goal = str(args.get("goal", "")).strip()
        if not goal:
            return "Ошибка: что делать?"
        await self.core.on_message(Incoming(ref=f"call:{uuid.uuid4().hex[:8]}", chat="owner", sender="owner",
                                            text="[голосом в звонке] " + goal))
        return "Принял, сделаю и отвечу в чате."

    def choose(self, peer: int) -> Persona | None:
        """An incoming call: the owner is welcome, known people get a short talk, spam and strangers over the limit not."""
        if peer == self.cfg.owner_id:
            return self.owner_persona("Ван позвонил тебе. Поздоровайся одним-двумя словами, например «слушаю».")
        if peer == SERVICE:
            return None
        key = self.key_of(peer)
        person = self.store.person(key)
        if person and person.blocked:
            return None
        recent = [t for t in self._incoming.get(peer, []) if time.time() - t < 3600]
        if len(recent) >= INCOMING_PER_HOUR:
            return None
        self._incoming[peer] = recent + [time.time()]

        async def run_tool(name: str, args: dict) -> str:
            if name != "pass_to_owner":
                return "Ошибка: этого ты не можешь."
            self._notes.setdefault(peer, []).append(str(args.get("note", ""))[:500])
            return "Передам."

        who = person.label if person else "незнакомец"
        system = "\n\n".join([persona.IDENTITY, persona.OTHERS.format(who=who, profile=person.profile if person else ""),
                              CALL_RULES, STRANGER_CALL])
        return Persona(system, "Тебе позвонил человек. Поздоровайся: ты Джарвис, ИИ-помощник Вана; спроси, чем помочь.",
                       [PASS_NOTE, HANG_UP], run_tool)

    def _outgoing(self, name: str, mode: str, goal: str) -> Persona:
        if mode == "say":
            system = (f"Ты Джарвис, ИИ-помощник Вана. Ты сам позвонил человеку ({name}), чтобы передать сообщение от Вана. "
                      f"{CALL_RULES}\nСкажи ему, что звонишь от Вана, и передай: {goal}. Если что-то спросит, отвечай "
                      "коротко. Передал, попрощался — hang_up.")
            opener = f"Ты позвонил. Поздоровайся, скажи что ты Джарвис от Вана и передай: {goal}"
        else:
            system = (f"Ты Джарвис, ИИ-помощник Вана. Ты сам позвонил человеку ({name}) по просьбе Вана: {goal}. "
                      f"{CALL_RULES}\nПредставься (Джарвис, помощник Вана; если спросят, ты ИИ, не отрицаешь), "
                      "передай привет и поговори живо и по-человечески о том, что ему интересно, о делах, о новостях. "
                      "Не дольше пяти минут, потом тепло попрощайся и вызови hang_up.")
            opener = f"Ты позвонил. Поздоровайся, скажи что ты Джарвис от Вана, передай привет и заведи разговор: {goal}"
        return Persona(system, opener, [HANG_UP])

    # ---- placing a call ----------------------------------------------------------------------------------------

    async def call(self, person_key: str, mode: str, goal: str) -> CallResult:
        if self.calls is None:
            raise TGError("звонки в Telegram сейчас не работают")
        peer = self.peer_of(person_key)
        if not peer:
            raise TGError("не знаю, кому звонить")
        person = self.store.person(person_key)
        name = person.name if person else person_key
        persona_ = (self.owner_persona(f"Ты сам позвонил Вану. Скажи ему: {goal}") if person_key == "owner"
                    else self._outgoing(name, mode, goal))
        done = asyncio.get_running_loop().create_future()
        self._ended[peer] = done
        self._placed.add(peer)
        try:
            picked = await self.calls.call(peer, persona_)
            if not picked:
                return CallResult(False, f"{name} не взял трубку.")
            limit = MAX_PLACED_SECONDS.get(mode, 120) if person_key != "owner" else self.cfg.call_minutes * 60
            try:
                talk: LiveTalk = await asyncio.wait_for(asyncio.shield(done), limit)
            except asyncio.TimeoutError:
                await self.calls.hang_up(peer)
                talk = await asyncio.wait_for(done, 20) if not done.done() else done.result()
            said = lines_of(talk.transcript, name)
            if mode == "say" and person_key != "owner":
                text = "Дозвонился, сказал и положил трубку." + (f"\n{said}"[:700] if said else "")
            else:
                text = f"Дозвонился, поговорили {max(1, round(talk.minutes))} мин." + (f"\n{said}"[:900] if said else "")
            return CallResult(True, text, talk.transcript)
        except asyncio.CancelledError:
            await self.calls.hang_up(peer)  # «стоп» during a call hangs up
            raise
        finally:
            self._ended.pop(peer, None)
            self._placed.discard(peer)

    async def ring_owner(self, text: str) -> bool:
        """An alarm or a reminder that rings: returns as soon as he picks up (the talk goes on by itself)."""
        if self.calls is None:
            return False
        opener = f"Ты сам позвонил Вану. Скажи ему сразу, без «привет, как дела»: {text}"
        return await self.calls.call(self.cfg.owner_id, self.owner_persona(opener))

    # ---- after a call ------------------------------------------------------------------------------------------

    async def ended(self, peer: int, talk: LiveTalk) -> None:
        fut = self._ended.get(peer)
        if fut and not fut.done():
            fut.set_result(talk)
        key = self.key_of(peer)
        minutes = max(1, round(talk.minutes))
        owner = peer == self.cfg.owner_id
        person = self.store.person(key)
        name = person.name if person else key
        said = lines_of(talk.transcript, "Ван" if owner else name)
        if said:
            self.store.add_msg(f"call:{peer}:{int(time.time())}", key, "jarvis" if owner else key,
                               f"[звонок, {minutes} мин]\n{said}"[:4000])
        notes = self._notes.pop(peer, [])
        if not self.account.tell_owner:
            return
        if owner:
            if said and fut is None:  # he rang me: the talk goes to the chat; a call I placed answers in its own task
                await self.account.tell_owner(f"📞 Звонок, {minutes} мин:\n{said}"[:4000])
            return
        if fut is not None and not notes:
            return  # the owner asked for this call: the task's own answer says how it went
        text = f"📞 Мне звонил {name}, {minutes} мин."
        if said:
            text += f"\n{said[:1500]}"
        if notes:
            text += "\n📨 Просил передать: " + "; ".join(notes)
        await self.account.tell_owner(text)

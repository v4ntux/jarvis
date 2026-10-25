"""Telegram voice calls of Jarvis's own account, through Gemini Live.

The call's sound (48 kHz mono PCM from py-tgcalls) streams to a Gemini Live session at 16 kHz.
Gemini listens, understands and answers with its own voice at 24 kHz, which plays back into
the call. When the other side talks over Jarvis, Gemini says so and the queued sound is dropped.
Functions let it act: the owner's calls can do what the free chat can, everyone else's can only
pass a note on.
"""
import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import aiohttp
import numpy as np

log = logging.getLogger("jarvis.calls")

RATE = 48000
FRAME = RATE // 100 * 2  # bytes in 10 ms of 16-bit mono
IN_RATE, OUT_RATE = 16000, 24000
RING_SECONDS = 45
SEND_EVERY = 0.04
HANG_UP_AFTER = 8
MIC_KEEP = IN_RATE * 2  # bytes: at most a second of sound waits for the session to open
LIVE_URL = ("wss://generativelanguage.googleapis.com/ws/"
            "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")


@dataclass
class Persona:
    """Who Jarvis is in this call and what it may do."""
    system: str
    opener: str  # a cue for the first words, e.g. «Поздоровайся» or «Скажи: …»
    tools: list[dict] = field(default_factory=list)  # Gemini function declarations
    run_tool: Callable[[str, dict], Awaitable[str]] | None = None


def down(pcm: bytes) -> bytes:
    """48 kHz → 16 kHz; averaging three samples also filters what plain decimation would alias."""
    audio = np.frombuffer(pcm[: len(pcm) // 6 * 6], np.int16).astype(np.int32)
    return audio.reshape(-1, 3).mean(axis=1).astype(np.int16).tobytes()


def up(pcm: bytes) -> bytes:
    """24 kHz → 48 kHz by linear interpolation."""
    audio = np.frombuffer(pcm[: len(pcm) // 2 * 2], np.int16).astype(np.float32)
    if not len(audio):
        return b""
    out = np.empty(len(audio) * 2, np.float32)
    out[0::2] = audio
    out[1::2] = (audio + np.append(audio[1:], audio[-1])) / 2
    return out.astype(np.int16).tobytes()


class LiveTalk:
    """One call bridged to one Gemini Live session."""

    def __init__(self, calls: "Calls", peer: int, persona: Persona):
        self.calls, self.peer, self.persona = calls, peer, persona
        self.started = time.monotonic()
        self.mic = bytearray()  # the other side, 16 kHz, waiting to be sent
        self.speaker = bytearray()  # Jarvis's voice, 48 kHz, waiting to be played
        self.transcript: list[tuple[str, str]] = []
        self.heard, self.said = "", ""
        self.ending = False
        self.task: asyncio.Task | None = None
        self._send: Callable[[dict], Awaitable[None]] | None = None
        self._tools: set[asyncio.Task] = set()

    @property
    def minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def hear(self, pcm: bytes) -> None:
        self.mic += down(pcm)
        if len(self.mic) > MIC_KEEP and self._send is None:
            del self.mic[:-MIC_KEEP]

    def setup(self, model: str) -> dict:
        p = self.persona
        setup = {"model": f"models/{model}",
                 "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {
                     "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": self.calls.voice}}}},
                 "systemInstruction": {"parts": [{"text": p.system}]},
                 "inputAudioTranscription": {}, "outputAudioTranscription": {}}
        if p.tools:
            setup["tools"] = [{"functionDeclarations": p.tools}]
        return {"setup": setup}

    async def run(self, minutes: int) -> None:
        async with aiohttp.ClientSession() as http:
            ws = await self._connect(http)
            try:
                self._send = ws.send_json
                await self.cue(self.persona.opener)
                workers = [asyncio.create_task(self._sender()), asyncio.create_task(self._player())]
                try:
                    await asyncio.wait_for(self._receive(ws), minutes * 60)
                except asyncio.TimeoutError:
                    await self.cue("Время звонка вышло: скажи об этом одной фразой и попрощайся.")
                    await asyncio.wait_for(self._receive(ws, until_turn=True), 20)
                    await self._drain()
                finally:
                    for task in workers + list(self._tools):
                        task.cancel()
            finally:
                await ws.close()

    async def _connect(self, http: aiohttp.ClientSession):
        """The first model in the chain that opens a session."""
        errors = []
        for model in self.calls.models:
            try:
                ws = await http.ws_connect(LIVE_URL, params={"key": self.calls.key}, heartbeat=20, max_msg_size=0)
                await ws.send_json(self.setup(model))
                msg = await ws.receive(timeout=15)
                if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY) and \
                        "setupComplete" in json.loads(msg.data):
                    log.info("live session with %s on %s", self.peer, model)
                    return ws
                errors.append(f"{model}: {msg.type} {ws.close_code} {str(msg.data)[:200]}")
                await ws.close()
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                errors.append(f"{model}: {type(e).__name__}: {e}")
        raise RuntimeError("Gemini Live не открылся: " + "; ".join(errors))

    async def cue(self, text: str) -> None:
        if text and self._send:
            await self._send({"clientContent": {"turns": [{"role": "user", "parts": [{"text": text}]}],
                                                "turnComplete": True}})

    async def _receive(self, ws, until_turn: bool = False) -> None:
        async for msg in ws:
            if msg.type not in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                break
            if self.on_server(json.loads(msg.data)) == "turn" and (until_turn or self.ending):
                if self.ending:
                    await self._drain()
                return
        log.info("live session with %s closed: %s", self.peer, ws.close_code)

    def on_server(self, data: dict) -> str | None:
        """Handles one message from Gemini; returns «turn» when Jarvis has finished speaking."""
        content = data.get("serverContent") or {}
        for part in (content.get("modelTurn") or {}).get("parts", []):
            inline = part.get("inlineData")
            if inline and inline.get("data"):
                self.speaker += up(base64.b64decode(inline["data"]))
        if content.get("interrupted"):
            self.speaker.clear()  # talked over: stop at once and listen
        if (content.get("inputTranscription") or {}).get("text"):
            self._flush("jarvis")
            self.heard += content["inputTranscription"]["text"]
        if (content.get("outputTranscription") or {}).get("text"):
            self._flush("them")
            self.said += content["outputTranscription"]["text"]
        for call in (data.get("toolCall") or {}).get("functionCalls", []):
            task = asyncio.create_task(self._tool(call))
            self._tools.add(task)
            task.add_done_callback(self._tools.discard)
        if data.get("goAway"):
            log.info("live session with %s ends soon: %s", self.peer, data["goAway"])
        if content.get("turnComplete"):
            self._flush("them")
            self._flush("jarvis")
            return "turn"
        return None

    def _flush(self, who: str) -> None:
        text = (self.heard if who == "them" else self.said).strip()
        if text:
            self.transcript.append((who, text))
        if who == "them":
            self.heard = ""
        else:
            self.said = ""

    async def _tool(self, call: dict) -> None:
        name, args = call.get("name", ""), call.get("args") or {}
        if name == "hang_up":
            self.ending = True
            result = "Кладу трубку после прощания."
            # The goodbye may already have been said: do not wait for another turn forever
            asyncio.get_running_loop().call_later(HANG_UP_AFTER, self._end_now)
        elif self.persona.run_tool:
            try:
                result = await self.persona.run_tool(name, args)
            except Exception as e:
                log.exception("call tool %s failed", name)
                result = f"Ошибка: {type(e).__name__}: {e}"
        else:
            result = "Ошибка: здесь нет инструментов."
        if self._send:
            await self._send({"toolResponse": {"functionResponses": [
                {"id": call.get("id"), "name": name, "response": {"result": result[:4000]}}]}})

    async def _sender(self) -> None:
        while True:
            await asyncio.sleep(SEND_EVERY)
            if self.mic:
                chunk = bytes(self.mic)
                self.mic.clear()
                await self._send({"realtimeInput": {"audio": {"data": base64.b64encode(chunk).decode(),
                                                              "mimeType": f"audio/pcm;rate={IN_RATE}"}}})

    async def _player(self) -> None:
        loop = asyncio.get_running_loop()
        due = loop.time()
        while True:
            if len(self.speaker) < FRAME:
                await asyncio.sleep(0.01)
                due = loop.time()
                continue
            frame = bytes(self.speaker[:FRAME])
            del self.speaker[:FRAME]
            await self.calls.send_frame(self.peer, frame)
            due += 0.01
            ahead = due - loop.time()
            if ahead > 0.04:  # real time pace, about 40 ms ahead of the listener
                await asyncio.sleep(ahead - 0.04)

    def _end_now(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()

    async def _drain(self) -> None:
        """Let the last words play before hanging up."""
        for _ in range(500):
            if len(self.speaker) < FRAME:
                return
            await asyncio.sleep(0.02)


class Calls:
    """Private calls on top of a logged-in Telethon client."""

    def __init__(self, client, key: str, models: str, voice: str, choose: Callable[[int], Persona | None],
                 ended: Callable[[int, LiveTalk], Awaitable[None]], minutes: int):
        from pytgcalls import PyTgCalls

        self.app = PyTgCalls(client)
        self.key, self.voice, self.minutes = key, voice, minutes
        self.models = [m.strip() for m in models.split(",") if m.strip()]
        # choose(peer) → how to answer an incoming call, None to decline it
        self.choose, self.ended = choose, ended
        self.talks: dict[int, LiveTalk] = {}

    async def start(self) -> None:
        from pytgcalls import filters
        from pytgcalls.types import ChatUpdate, Direction

        self.app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))(self._incoming)
        self.app.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))(self._left)
        # Only audio is ever requested, so every incoming frame is the other side's voice
        self.app.on_update(filters.stream_frame(Direction.INCOMING))(self._frames)
        await self.app.start()

    async def call(self, peer: int, persona: Persona) -> bool:
        """Rings peer; True once they pick up (the talk goes on in the background), False if not."""
        if peer in self.talks:
            await self.talks[peer].cue(persona.opener)
            return True
        answered = asyncio.get_running_loop().create_future()
        asyncio.create_task(self._talk(peer, persona, answered))
        return await answered

    async def _talk(self, peer: int, persona: Persona, answered: asyncio.Future | None = None) -> None:
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters

        talk = LiveTalk(self, peer, persona)
        talk.task = asyncio.current_task()
        self.talks[peer] = talk
        connected = False
        try:
            params = AudioParameters(RATE, 1)
            await self.app.play(peer, MediaStream(ExternalMedia.AUDIO, audio_parameters=params),
                                CallConfig(timeout=RING_SECONDS))
            await self.app.record(peer, RecordStream(audio=True, audio_parameters=params))
            connected = True
            if answered and not answered.done():
                answered.set_result(True)
            await talk.run(self.minutes)
        except asyncio.CancelledError:
            pass  # the other side hung up
        except Exception as e:
            log.warning("call with %s failed: %s: %s", peer, type(e).__name__, e)
        finally:
            if answered and not answered.done():
                answered.set_result(False)
            self.talks.pop(peer, None)
            await self.hang_up(peer)
            if connected:
                try:
                    await self.ended(peer, talk)
                except Exception:
                    log.exception("call end handler failed")

    async def hang_up(self, peer: int) -> None:
        try:
            await self.app.leave_call(peer)
        except Exception:
            pass  # already gone

    async def send_frame(self, peer: int, frame: bytes) -> None:
        from pytgcalls.types import Device

        await self.app.send_frame(peer, Device.MICROPHONE, frame)

    async def _incoming(self, _, update) -> None:
        peer = update.chat_id
        persona = None if peer in self.talks else self.choose(peer)
        if persona is None:
            log.info("declining call from %s", peer)
            await self.hang_up(peer)
            return
        log.info("answering call from %s", peer)
        asyncio.create_task(self._talk(peer, persona))

    async def _left(self, _, update) -> None:
        talk = self.talks.get(update.chat_id)
        if talk and talk.task and talk.task is not asyncio.current_task():
            talk.task.cancel()

    async def _frames(self, _, update) -> None:
        talk = self.talks.get(update.chat_id)
        if talk:
            for frame in update.frames:
                talk.hear(frame.frame)

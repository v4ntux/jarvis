"""Telegram voice calls of Jarvis's own account: answer, ring someone, talk.

Sound goes both ways as raw 48 kHz mono PCM through py-tgcalls. The other side's voice
is cut into phrases by loudness, recognized by Whisper, answered by whoever the account
decides (the brain for the owner, Gemini for everyone else) and spoken back with Edge TTS.
"""
import asyncio
import collections
import logging
import re
import time
from typing import Awaitable, Callable

import numpy as np

from . import voice

log = logging.getLogger("jarvis.calls")

RATE = 48000
FRAME = RATE // 100 * 2  # bytes in 10 ms of 16-bit mono
RING_SECONDS = 45
SILENCE_HANGUP = 90
FILLER_AFTER = 4
BYE = re.compile(r"(пока|отбой|до свидания|до связи|клади трубку|положи трубку|конец связи|отключайся)[\s.!]*$",
                 re.I)

Reply = Callable[[str], Awaitable[str]]


class Endpointer:
    """Cuts 16-bit mono PCM into phrases by loudness.

    A phrase starts after START_MS of sound and ends after END_MS of quiet. The noise level is
    the quietest 5% of the last FLOOR_MS: pauses between words keep it at the real background,
    while steady noise raises it within seconds.
    """
    START_MS = 150
    END_MS = 800
    PREROLL_MS = 300
    MIN_VOICED_MS = 250
    MAX_MS = 30_000
    MIN_RMS = 300.0
    FLOOR_MS = 5000

    def __init__(self, rate: int = RATE):
        self.size = rate // 100  # samples in 10 ms
        # Starts as one second of quiet line, so speech right away is not taken for noise
        self.levels: collections.deque = collections.deque([self.MIN_RMS / 3] * 100, maxlen=self.FLOOR_MS // 10)
        self.rest = np.zeros(0, np.int16)
        self.preroll: collections.deque = collections.deque(maxlen=self.PREROLL_MS // 10)
        self.phrase: list[np.ndarray] | None = None
        self.loud_run = self.quiet_run = self.voiced = 0

    @property
    def talking(self) -> bool:
        """The other side has been speaking for a moment now: Jarvis should stop and listen."""
        return self.phrase is not None and self.quiet_run < 30 and self.voiced >= 40

    def feed(self, pcm: bytes) -> list[bytes]:
        samples = np.concatenate([self.rest, np.frombuffer(pcm, np.int16)])
        count = len(samples) // self.size
        self.rest = samples[count * self.size:]
        done = []
        for i in range(count):
            frame = samples[i * self.size:(i + 1) * self.size]
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            self.levels.append(rms)
            loud = rms > max(float(np.percentile(self.levels, 5)) * 3, self.MIN_RMS)
            if self.phrase is None:
                self.preroll.append(frame)
                self.loud_run = self.loud_run + 1 if loud else 0
                if self.loud_run * 10 >= self.START_MS:
                    self.phrase, self.voiced, self.quiet_run = list(self.preroll), self.loud_run, 0
                    self.preroll.clear()
                continue
            self.phrase.append(frame)
            if loud:
                self.voiced, self.quiet_run = self.voiced + 1, 0
            else:
                self.quiet_run += 1
            if self.quiet_run * 10 >= self.END_MS or len(self.phrase) * 10 >= self.MAX_MS:
                if self.voiced * 10 >= self.MIN_VOICED_MS:
                    keep = len(self.phrase) - max(0, self.quiet_run - 20)  # 200 ms of the tail is enough
                    done.append(np.concatenate(self.phrase[:keep]).tobytes())
                self.phrase, self.loud_run = None, 0
        return done


class Conversation:
    """One call: listen, answer, speak, until someone hangs up or says goodbye."""

    def __init__(self, calls: "Calls", peer: int, reply: Reply, opener: str, minutes: int):
        self.calls, self.peer, self.reply, self.opener = calls, peer, reply, opener
        self.deadline = time.monotonic() + minutes * 60
        self.started = time.monotonic()
        self.ears = Endpointer()
        self.phrases: asyncio.Queue[bytes] = asyncio.Queue()
        self.mouth: asyncio.Task | None = None
        self.task: asyncio.Task | None = None
        self.transcript: list[tuple[str, str]] = []

    @property
    def minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def hear(self, pcm: bytes) -> None:
        for phrase in self.ears.feed(pcm):
            self.phrases.put_nowait(phrase)
        if self.mouth and not self.mouth.done() and self.ears.talking:
            self.mouth.cancel()  # interrupted: stop talking and listen

    async def say(self, text: str) -> None:
        if not text:
            return
        self.transcript.append(("jarvis", text))
        self.mouth = asyncio.create_task(self._speak(text))
        try:
            await asyncio.wait({self.mouth})
        finally:
            self.mouth.cancel()

    async def _speak(self, text: str) -> None:
        # Every sentence is synthesized at once, and the first one plays as soon as it is ready
        parts = [asyncio.create_task(voice.synthesize_pcm(s, RATE)) for s in voice.sentences(text)]
        try:
            for part in parts:
                await self.calls.play_pcm(self.peer, await part)
        except Exception:
            log.exception("speaking failed")
        finally:
            for part in parts:
                part.cancel()

    async def run(self) -> None:
        await self.say(self.opener)
        while True:
            left = self.deadline - time.monotonic()
            if left <= 0:
                await self.say("Время звонка вышло, продолжим в переписке.")
                return
            try:
                phrase = await asyncio.wait_for(self.phrases.get(), min(left, SILENCE_HANGUP))
            except asyncio.TimeoutError:
                if time.monotonic() < self.deadline:
                    await self.say("Не слышу тебя, отключаюсь.")
                    return
                continue
            while not self.phrases.empty():  # phrases that came in meanwhile belong together
                phrase += self.phrases.get_nowait()
            text = await voice.transcribe_pcm(phrase, RATE)
            if not text:
                continue
            self.transcript.append(("them", text))
            if BYE.search(text) and len(text) < 60:
                await self.say("До связи.")
                return
            await self.say(await self._answer(text))

    async def _answer(self, text: str) -> str:
        # The reply keeps going even if the call drops: the owner still gets it in the chat
        task = asyncio.create_task(self.reply(text))
        done, _ = await asyncio.wait({task}, timeout=FILLER_AFTER)
        if not done:
            await self.say("Секунду.")
            await asyncio.wait({task})
        try:
            return task.result()
        except Exception:
            log.exception("call reply failed")
            return "Что-то сломалось, попробуй ещё раз."


class Calls:
    """Private calls on top of a logged-in Telethon client."""

    def __init__(self, client, choose: Callable[[int], tuple[Reply, str] | None],
                 ended: Callable[[int, Conversation], Awaitable[None]], minutes: int):
        from pytgcalls import PyTgCalls

        self.app = PyTgCalls(client)
        # choose(peer) → (reply, opener) to answer an incoming call, None to decline it
        self.choose, self.ended, self.minutes = choose, ended, minutes
        self.talks: dict[int, Conversation] = {}
        self._frame_logged = False

    async def start(self) -> None:
        from pytgcalls import filters
        from pytgcalls.types import ChatUpdate, Direction

        self.app.on_update(filters.chat_update(ChatUpdate.Status.INCOMING_CALL))(self._incoming)
        self.app.on_update(filters.chat_update(ChatUpdate.Status.LEFT_CALL))(self._left)
        # Only audio is ever requested, so every incoming frame is the other side's voice
        self.app.on_update(filters.stream_frame(Direction.INCOMING))(self._frames)
        await self.app.start()

    def busy(self, peer: int) -> bool:
        return peer in self.talks

    async def call(self, peer: int, opener: str, reply: Reply) -> bool:
        """Rings peer; True once they pick up (the talk goes on in the background), False if not."""
        if peer in self.talks:
            await self.talks[peer].say(opener)
            return True
        answered = asyncio.get_running_loop().create_future()
        asyncio.create_task(self._talk(peer, reply, opener, answered))
        return await answered

    async def _talk(self, peer: int, reply: Reply, opener: str, answered: asyncio.Future | None = None) -> None:
        from pytgcalls.types import CallConfig, ExternalMedia, MediaStream, RecordStream
        from pytgcalls.types.raw import AudioParameters

        talk = Conversation(self, peer, reply, opener, self.minutes)
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
            await talk.run()
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

    async def play_pcm(self, peer: int, pcm: bytes) -> None:
        from pytgcalls.types import Device

        loop = asyncio.get_running_loop()
        start = loop.time()
        for n, i in enumerate(range(0, len(pcm) - FRAME + 1, FRAME)):
            ahead = start + n * 0.01 - loop.time()
            if ahead > 0.06:  # real time pace, about 40 ms ahead of the listener
                await asyncio.sleep(ahead - 0.04)
            await self.app.send_frame(peer, Device.MICROPHONE, pcm[i:i + FRAME])

    async def _incoming(self, _, update) -> None:
        peer = update.chat_id
        choice = None if peer in self.talks else self.choose(peer)
        if choice is None:
            log.info("declining call from %s", peer)
            await self.hang_up(peer)
            return
        log.info("answering call from %s", peer)
        asyncio.create_task(self._talk(peer, *choice))

    async def _left(self, _, update) -> None:
        talk = self.talks.get(update.chat_id)
        if talk and talk.task and talk.task is not asyncio.current_task():
            talk.task.cancel()

    async def _frames(self, _, update) -> None:
        talk = self.talks.get(update.chat_id)
        if not talk:
            return
        for frame in update.frames:
            if not self._frame_logged:
                self._frame_logged = True
                log.info("first call frame: %d bytes", len(frame.frame))
            talk.hear(frame.frame)

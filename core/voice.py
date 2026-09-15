"""Голос: синтез речи Microsoft Edge + кеш на диске.

Кеш важен для скорости: «Готово», «Слушаю», «Открываю» звучат десятки раз в день,
и повторно синтезировать их незачем — уже готовый звук стартует мгновенно.
"""
import asyncio
import hashlib
import io
import os
import re
import threading
import tempfile
import time
from pathlib import Path

import numpy as np
try:
    import sounddevice as sd
except (ImportError, OSError):
    sd = None

from core import settings as cfg

CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Jarvis" / "voice"
RATE = 24000
SYNTHESIS_TIMEOUT = 12.0
NETWORK_RETRY_AFTER = 30.0

_CODE = re.compile(r"```.*?```", re.S)
_URL = re.compile(r"https?://\S+")
_JUNK = re.compile(r"[*_#>`|]+")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\U00002600-\U000027BF]+")


def clean(text):
    """Убирает из текста то, что нельзя произнести."""
    text = _CODE.sub(" ", text or "")
    text = _URL.sub(" ссылка ", text)
    text = _EMOJI.sub("", text)
    text = _JUNK.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class SpeechCancelled(Exception):
    """An interrupted response must never be spoken later."""


class Voice:
    """Speech with bounded synthesis, isolated output and observable failures."""

    def __init__(self):
        self.enabled = cfg.TTS_ENABLED
        self.name = cfg.VOICE
        self.rate = cfg.VOICE_RATE
        self.pitch = cfg.VOICE_PITCH
        self._lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._speaking = threading.Event()
        self._generation = 0
        self._output = None
        self._edge_retry_at = 0.0
        self.last_error = ""
        self.last_backend = "idle"
        self.last_success = False
        try:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    # --- синтез -------------------------------------------------------------
    def _cache_file(self, text):
        key = "|".join((text, self.name, self.rate, self.pitch))
        return CACHE_DIR / (hashlib.sha1(key.encode("utf-8")).hexdigest() + ".pcm")

    def refresh_settings(self):
        """Apply voice preferences between utterances, without synthesis/playback."""
        self.stop()
        with self._lock:
            self.enabled = cfg.TTS_ENABLED
            self.name = cfg.VOICE
            self.rate = cfg.VOICE_RATE
            self.pitch = cfg.VOICE_PITCH
            self._edge_retry_at = 0.0
            self.last_error = ""
            self.last_backend = "idle" if self.enabled else "disabled"
            self.last_success = False
            if not self.name or not re.fullmatch(r"[+-]\d+%", self.rate or "") \
                    or not re.fullmatch(r"[+-]\d+Hz", self.pitch or ""):
                self.last_error = "Неверные параметры голоса. Выберите голос, скорость и тембр в настройках."
                return False
            return True

    async def _synthesize(self, text):
        import edge_tts

        buf = io.BytesIO()
        comm = edge_tts.Communicate(text, self.name, rate=self.rate, pitch=self.pitch,
                                   connect_timeout=5, receive_timeout=8)
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    async def _synthesize_bounded(self, text, cancelled=None):
        task = asyncio.create_task(self._synthesize(text))
        deadline = time.monotonic() + SYNTHESIS_TIMEOUT
        try:
            while not task.done():
                if cancelled and cancelled():
                    raise SpeechCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Сервис голоса не ответил за %g секунд" % SYNTHESIS_TIMEOUT)
                await asyncio.wait({task}, timeout=min(0.1, remaining))
            return await task
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    @staticmethod
    def _decode(mp3):
        import av

        parts = []
        with av.open(io.BytesIO(mp3)) as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
            for frame in container.decode(container.streams.audio[0]):
                for out in resampler.resample(frame):
                    parts.append(out.to_ndarray().reshape(-1))
            for out in resampler.resample(None):
                parts.append(out.to_ndarray().reshape(-1))
        return np.concatenate(parts) if parts else np.zeros(0, dtype=np.int16)

    def _pcm_for(self, text, cancelled=None):
        """Готовый звук: из кеша, иначе синтезируем и кладём в кеш."""
        path = self._cache_file(text)
        if path.exists():
            try:
                cached = np.fromfile(path, dtype=np.int16)
                if len(cached):
                    return cached
            except (OSError, ValueError):
                pass
        if cancelled and cancelled():
            raise SpeechCancelled()
        if time.monotonic() < self._edge_retry_at:
            raise RuntimeError("Сетевой голос временно недоступен")
        try:
            pcm = self._decode(asyncio.run(self._synthesize_bounded(text, cancelled)))
            if not len(pcm):
                raise RuntimeError("Сервис голоса вернул пустой звук")
            self._edge_retry_at = 0.0
        except SpeechCancelled:
            raise
        except Exception:
            self._edge_retry_at = time.monotonic() + NETWORK_RETRY_AFTER
            raise
        temporary = None
        try:
            if len(pcm) and len(pcm) < RATE * 30:      # длинные ответы не копим
                with tempfile.NamedTemporaryFile(dir=CACHE_DIR, suffix=".tmp", delete=False) as handle:
                    temporary = Path(handle.name)
                    pcm.tofile(handle)
                os.replace(temporary, path)
        except OSError:
            pass
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return pcm

    # --- воспроизведение ----------------------------------------------------
    def say(self, text, wait=True):
        """Return whether speech played; asynchronous mode reports queue acceptance."""
        if not self.enabled:
            self.last_backend = "disabled"
            self.last_success = False
            return False
        speech = clean(text)
        if not speech:
            return False
        with self._state_lock:
            generation = self._generation
        if not wait:
            threading.Thread(target=self._say, args=(speech, generation), daemon=True).start()
            return True
        return self._say(speech, generation)

    def _say(self, speech, generation):
        cancelled = lambda: generation != self._generation
        with self._lock:
            if cancelled():
                return False
            self._stop.clear()
            self._speaking.set()
            self.last_error = ""
            self.last_success = False
            try:
                pcm = self._pcm_for(speech, cancelled=cancelled)
                if cancelled():
                    raise SpeechCancelled()
                self._play(pcm, cancelled)
                self.last_backend = "edge"
                self.last_success = True
                return True
            except SpeechCancelled:
                self.last_backend = "stopped"
                return False
            except Exception as primary_error:
                if cancelled():
                    self.last_backend = "stopped"
                    return False
                try:
                    self._sapi(speech, cancelled)
                    self.last_backend = "sapi"
                    self.last_success = True
                    return True
                except SpeechCancelled:
                    self.last_backend = "stopped"
                    return False
                except Exception as fallback_error:
                    self.last_backend = "unavailable"
                    self.last_error = "Голос: %s. Резерв Windows: %s" % (primary_error, fallback_error)
                    print("  [%s]" % self.last_error, flush=True)
                    return False
            finally:
                self._speaking.clear()

    def _play(self, pcm, cancelled):
        if sd is None:
            raise RuntimeError("Аудиовыход sounddevice недоступен")
        # Never use sd.play/sd.stop: their shared stream can stop microphone capture.
        stream = sd.OutputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=1200)
        try:
            with self._state_lock:
                if cancelled():
                    raise SpeechCancelled()
                self._output = stream
            with stream:
                for index in range(0, len(pcm), 1200):
                    if cancelled():
                        raise SpeechCancelled()
                    stream.write(pcm[index:index + 1200].reshape(-1, 1))
            if cancelled():
                raise SpeechCancelled()
        except Exception:
            if cancelled():
                raise SpeechCancelled()
            raise
        finally:
            with self._state_lock:
                if self._output is stream:
                    self._output = None
            stream.close()

    def _sapi(self, text, cancelled):
        """Offline Windows voice fallback when Edge TTS or decoding is unavailable."""
        if cancelled():
            raise SpeechCancelled()
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            if re.search(r"[а-яё]", text, re.I):
                russian = []
                for item in speaker.GetVoices():
                    languages = str(item.GetAttribute("Language")).lower().split(";")
                    if "419" in languages or "russian" in item.GetDescription().lower():
                        russian.append(item)
                if not russian:
                    raise RuntimeError("В Windows не установлен русский голос. Добавьте русский язык речи в настройках Windows.")
                speaker.Voice = russian[0]
            speaker.Rate = 1
            speaker.Speak(text, 1)  # SVSFlagsAsync
            deadline = time.monotonic() + max(15.0, min(180.0, len(text) / 5.0))
            while not speaker.WaitUntilDone(50):
                if cancelled():
                    speaker.Speak("", 3)  # Async + purge queued speech
                    raise SpeechCancelled()
                if time.monotonic() >= deadline:
                    speaker.Speak("", 3)
                    raise TimeoutError("Голос Windows не завершил воспроизведение")
            if cancelled():
                raise SpeechCancelled()
        finally:
            pythoncom.CoUninitialize()

    def stop(self):
        self._stop.set()
        with self._state_lock:
            self._generation += 1
            output = self._output
        if output is not None:
            try:
                output.abort()
            except Exception:
                pass

    @property
    def speaking(self):
        return self._speaking.is_set()

    def prewarm(self, phrases):
        """Заранее синтезирует частые фразы, чтобы первый раз тоже был мгновенным."""
        if not self.enabled:
            return
        for phrase in phrases:
            if self.speaking or self._stop.is_set():
                return
            try:
                self._pcm_for(clean(phrase), cancelled=lambda: self.speaking or self._stop.is_set())
            except Exception:
                return

    def health(self):
        return {"enabled": self.enabled, "speaking": self.speaking,
                "backend": self.last_backend, "error": self.last_error,
                "success": self.last_success}

    def cache_size(self):
        try:
            return sum(f.stat().st_size for f in CACHE_DIR.glob("*.pcm"))
        except OSError:
            return 0

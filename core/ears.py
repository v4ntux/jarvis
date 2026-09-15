"""Strict local wake gate, active-session recording and STT routing."""
import difflib
import math
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
try:
    import sounddevice as sd
except (ImportError, OSError):
    sd = None

from core import settings as cfg


BLOCK = 0.03
WAKE_BLOCK = 0.08
MIN_SPEECH = 0.30
PARTIAL_EVERY = 0.8
INPUT_STALL_TIMEOUT = 3.0
PRE_ROLL = 0.24

_TRANSLIT = {
    "дж": "j", "ж": "j", "ч": "ch", "ш": "sh", "щ": "sh", "ц": "c",
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "з": "z",
    "и": "i", "й": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
    "ы": "y", "э": "e", "ю": "u", "я": "a", "ь": "", "ъ": "",
}


def canon(word):
    w = (word or "").lower().replace("ё", "е")
    out, index = [], 0
    while index < len(w):
        pair = w[index:index + 2]
        if pair in _TRANSLIT:
            out.append(_TRANSLIT[pair])
            index += 2
        elif w[index] in _TRANSLIT:
            out.append(_TRANSLIT[w[index]])
            index += 1
        elif w[index].isalpha():
            out.append(w[index])
            index += 1
        else:
            index += 1
    return "".join(out)


def looks_like_name(word):
    candidate = canon(word)
    target = canon(cfg.NAME) or "jarvis"
    if len(candidate) < 4:
        return False
    return difflib.SequenceMatcher(None, candidate, target).ratio() >= cfg.WAKE_FUZZY


def split_wake(text, window=3):
    words = re.findall(r"[^\W\d_]+", text or "", flags=re.UNICODE)
    for _index, word in enumerate(words[:window]):
        if looks_like_name(word):
            position = text.lower().find(word.lower())
            rest = text[position + len(word):] if position >= 0 else ""
            return True, rest.lstrip(" ,.!?-—:")
    return False, (text or "").strip()


def normalize_audio(audio):
    if audio is None or len(audio) == 0:
        return audio
    peak = float(np.max(np.abs(audio)))
    if peak < 1e-4:
        return audio
    return np.clip(audio * (0.85 / peak), -1.0, 1.0).astype(np.float32)


@dataclass
class WakeEvent:
    audio: np.ndarray
    score: float = 0.0
    engine: str = "openwakeword"


class Ears:
    """Microphone services. Sleeping and active paths are intentionally separate."""

    def __init__(self):
        self.device = self._pick_device(cfg.MIC)
        self.noise_floor = 0.004
        self._main = None
        self._wake = None
        self._main_name = None
        self._wake_name = None
        self._wake_detector = None
        self._wake_error = ""
        self._main_used = 0.0
        self._wake_used = 0.0
        self._lock = threading.RLock()
        self._inference_lock = threading.Lock()
        self._partial_lock = threading.Lock()
        self._final_transcribing = threading.Event()
        self.vocabulary = None
        self.last_stt = "local"
        self.last_error = ""
        self._selected_wake_engine = None

    @staticmethod
    def _pick_device(value):
        if not value:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return str(value)

    def _input_stream(self, **kwargs):
        if sd is None:
            raise RuntimeError("Микрофон недоступен: установите sounddevice и проверьте аудиодрайвер.")
        return sd.InputStream(device=self.device, **kwargs)

    def refresh_settings(self):
        """Apply settings on the runtime owner thread, without opening audio."""
        with self._inference_lock, self._lock:
            selected = self._pick_device(cfg.MIC)
            if selected != self.device:
                self.noise_floor = 0.004
            self.device = selected
            if self._main_name != cfg.MODEL_MAIN:
                self._main = None
                self._main_name = None
            if self._wake_name != cfg.MODEL_WAKE:
                self._wake = None
                self._wake_name = None
            self._wake_detector = None
            self._selected_wake_engine = None
            self.vocabulary = None
            self._wake_error = ""
            self.last_error = ""
        try:
            if sd is None:
                raise RuntimeError("Аудиодрайвер sounddevice недоступен")
            if isinstance(self.device, int) and self.device < 0:
                raise ValueError("Номер микрофона должен быть неотрицательным или пустым")
            sd.check_input_settings(device=self.device, samplerate=cfg.SAMPLE_RATE,
                                    channels=1, dtype="float32")
        except Exception as exc:
            self.last_error = "Выбранный микрофон недоступен: %s" % exc
            return False
        return True

    @staticmethod
    def devices():
        out = []
        if sd is None:
            return out
        try:
            for index, info in enumerate(sd.query_devices()):
                if info["max_input_channels"] > 0:
                    out.append((index, info["name"]))
        except Exception:
            pass
        return out

    def _load(self, name):
        from faster_whisper import WhisperModel
        from huggingface_hub.errors import LocalEntryNotFoundError

        options = dict(device="cpu", compute_type="int8", cpu_threads=2)
        try:
            # Hugging Face otherwise checks remote metadata even for cached models.
            return WhisperModel(name, local_files_only=True, **options)
        except LocalEntryNotFoundError:
            raise RuntimeError(
                "Локальная модель %s ещё не установлена. Запустите prepare-voice.bat "
                "или python -m core.prepare_voice при наличии интернета."
                % name
            ) from None

    @property
    def main(self):
        with self._lock:
            if self._main is None:
                self._main = self._load(cfg.MODEL_MAIN)
                self._main_name = cfg.MODEL_MAIN
            self._main_used = time.time()
            return self._main

    @property
    def wake(self):
        with self._lock:
            if cfg.MODEL_WAKE == cfg.MODEL_MAIN:
                model = self._main or self._load(cfg.MODEL_MAIN)
                self._main = model
                self._main_name = cfg.MODEL_MAIN
                self._main_used = time.time()
                return model
            if self._wake is None:
                self._wake = self._load(cfg.MODEL_WAKE)
                self._wake_name = cfg.MODEL_WAKE
            self._wake_used = time.time()
            return self._wake

    def _openwakeword(self):
        with self._lock:
            if self._wake_detector is not None:
                return self._wake_detector
            from openwakeword.model import Model

            self._wake_detector = Model(
                wakeword_models=["hey jarvis"], inference_framework="onnx", vad_threshold=0.15
            )
            return self._wake_detector

    def release_idle(self):
        if cfg.UNLOAD_AFTER <= 0:
            return False
        now, freed = time.time(), False
        with self._lock:
            if self._main is not None and now - self._main_used > cfg.UNLOAD_AFTER:
                self._main = None
                freed = True
            if self._wake is not None and now - self._wake_used > cfg.UNLOAD_AFTER:
                self._wake = None
                freed = True
        if freed:
            import gc
            gc.collect()
        return freed

    def hint(self):
        if self.vocabulary is None:
            try:
                from core import actions
                names = ", ".join(actions.app_names()[:30])
            except Exception:
                names = ""
            self.vocabulary = (
                "%s. Команды: открой, закрой, таймер, напомни, заметка, громкость, "
                "яркость, скриншот, пауза, следующий трек. Программы: %s."
                % (cfg.NAME, names)
            )
        return self.vocabulary

    def calibrate(self, seconds=0.6, stop_flag=None):
        if stop_flag and stop_flag():
            return self.noise_floor
        try:
            duration = max(0.05, min(float(seconds), 1.5))
            frames = int(cfg.SAMPLE_RATE * duration)
            blocksize = int(cfg.SAMPLE_RATE * BLOCK)
            chunks = queue.Queue(maxsize=80)

            def callback(indata, _frames, _time_info, _status):
                try:
                    chunks.put_nowait(indata.copy())
                except queue.Full:
                    pass

            collected, received = [], 0
            deadline = time.monotonic() + min(2.0, duration + 0.75)
            with self._input_stream(samplerate=cfg.SAMPLE_RATE, channels=1,
                                    dtype="float32", blocksize=blocksize, callback=callback):
                while received < frames:
                    if stop_flag and stop_flag():
                        return self.noise_floor
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Микрофон не передал звук для калибровки")
                    try:
                        block = chunks.get(timeout=min(0.1, remaining)).reshape(-1)
                    except queue.Empty:
                        continue
                    collected.append(block)
                    received += len(block)
            recording = np.concatenate(collected)[:frames]
            self.noise_floor = max(
                float(np.sqrt(np.mean(recording.astype(np.float64) ** 2))), 0.0015
            )
            self.last_error = ""
        except Exception as exc:
            self.last_error = "Не удалось проверить микрофон: %s" % exc
            print("  [микрофон не откалиброван: %s]" % exc, flush=True)
        return self.noise_floor

    def threshold(self):
        return max(self.noise_floor * cfg.MIC_SENSITIVITY, 0.006)

    def wait_for_wake(self, stop_flag=None, on_level=None):
        """Return a candidate wake utterance without invoking full STT or cloud AI."""
        if stop_flag and stop_flag():
            return None
        # The bundled detector was trained on English "hey jarvis". Russian and
        # user-defined names need local transcription, not that unrelated gate.
        engine = cfg.WAKE_ENGINE
        if engine == "auto":
            engine = "whisper"
        self._selected_wake_engine = engine
        if engine == "whisper":
            return self._wait_whisper_candidate(stop_flag, on_level)
        try:
            return self._wait_openwakeword(stop_flag, on_level)
        except Exception as exc:
            self._wake_error = "%s: %s" % (type(exc).__name__, exc)
            self.last_error = "Wake-word недоступен: %s. Выберите режим auto." % self._wake_error
            raise RuntimeError(self.last_error) from exc

    def _wait_whisper_candidate(self, stop_flag=None, on_level=None):
        audio = self.record(max_wait=None, stop_flag=stop_flag, on_level=on_level)
        return WakeEvent(audio=audio, engine="whisper") if audio is not None else None

    def _wait_openwakeword(self, stop_flag=None, on_level=None):
        detector = self._openwakeword()
        blocksize = int(cfg.SAMPLE_RATE * WAKE_BLOCK)
        chunks = queue.Queue(maxsize=80)
        pre_roll = deque(maxlen=math.ceil(2.4 / WAKE_BLOCK))
        threshold = self.threshold()
        high_frames = 0

        def callback(indata, frames, time_info, status):
            try:
                chunks.put_nowait(indata.copy())
            except queue.Full:
                try:
                    chunks.get_nowait()
                    chunks.put_nowait(indata.copy())
                except queue.Empty:
                    pass

        last_block = time.monotonic()
        with self._input_stream(samplerate=cfg.SAMPLE_RATE, channels=1, dtype="float32",
                                blocksize=blocksize, callback=callback):
            while True:
                if stop_flag is not None and stop_flag():
                    return None
                try:
                    block = chunks.get(timeout=0.1)
                except queue.Empty:
                    if time.monotonic() - last_block >= INPUT_STALL_TIMEOUT:
                        raise RuntimeError("Микрофон перестал передавать звук. Проверьте устройство в Windows.")
                    continue
                last_block = time.monotonic()
                mono = block.reshape(-1)
                pre_roll.append(mono)
                level = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
                if on_level:
                    on_level(min(1.0, level / max(threshold * 4, 0.02)))
                pcm = (np.clip(mono, -1, 1) * 32767).astype(np.int16)
                prediction = detector.predict(pcm)
                score = max((float(value) for value in prediction.values()), default=0.0)
                high_frames = high_frames + 1 if score >= cfg.WAKE_THRESHOLD else 0
                if high_frames < cfg.WAKE_PATIENCE:
                    continue

                collected = list(pre_roll)
                silence = 0.0
                started_at = time.monotonic()
                best_score = score
                while True:
                    if stop_flag is not None and stop_flag():
                        return None
                    try:
                        follow = chunks.get(timeout=0.1).reshape(-1)
                    except queue.Empty:
                        if time.monotonic() - last_block >= INPUT_STALL_TIMEOUT:
                            raise RuntimeError("Микрофон перестал передавать звук.")
                        continue
                    last_block = time.monotonic()
                    collected.append(follow)
                    follow_level = float(np.sqrt(np.mean(follow.astype(np.float64) ** 2)))
                    if on_level:
                        on_level(min(1.0, follow_level / max(threshold * 4, 0.02)))
                    silence = silence + WAKE_BLOCK if follow_level <= threshold else 0.0
                    if silence >= cfg.SILENCE_TAIL or time.monotonic() - started_at >= cfg.MAX_PHRASE:
                        break
                detector.reset()
                audio = np.concatenate(collected).astype(np.float32)
                return WakeEvent(audio=audio, score=best_score, engine="openwakeword")

    def record(self, max_wait=None, stop_flag=None, on_level=None,
               on_partial=None, on_speech_start=None):
        recording = threading.Event()
        recording.set()
        try:
            audio = self._record(max_wait, stop_flag, on_level, on_partial,
                                 on_speech_start, recording)
            self.last_error = ""
            return audio
        except Exception as exc:
            self.last_error = "Микрофон недоступен: %s" % exc
            raise RuntimeError(self.last_error) from exc
        finally:
            recording.clear()

    def _record(self, max_wait, stop_flag, on_level, on_partial,
                on_speech_start, recording):
        if stop_flag and stop_flag():
            return None
        threshold = self.threshold()
        blocksize = int(cfg.SAMPLE_RATE * BLOCK)
        chunks = queue.Queue(maxsize=100)
        pre_roll = deque(maxlen=max(1, math.ceil(PRE_ROLL / BLOCK)))

        def callback(indata, frames, time_info, status):
            try:
                chunks.put_nowait(indata.copy())
            except queue.Full:
                try:
                    chunks.get_nowait()
                except queue.Empty:
                    pass
                try:
                    chunks.put_nowait(indata.copy())
                except queue.Full:
                    pass

        collected, started = [], False
        silence = 0.0
        started_at = last_partial = None
        opened_at = last_block = time.monotonic()
        with self._input_stream(samplerate=cfg.SAMPLE_RATE, channels=1, dtype="float32",
                                blocksize=blocksize, callback=callback):
            while True:
                if stop_flag is not None and stop_flag():
                    return None
                now = time.monotonic()
                if not started and max_wait is not None and now - opened_at >= max_wait:
                    return None
                try:
                    mono = chunks.get(timeout=0.1).reshape(-1)
                except queue.Empty:
                    if time.monotonic() - last_block >= INPUT_STALL_TIMEOUT:
                        raise RuntimeError("Нет аудиоданных. Выберите работающий микрофон в настройках Windows.")
                    continue
                last_block = time.monotonic()
                level = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2)))
                if on_level:
                    on_level(min(1.0, level / max(threshold * 4, 0.02)))
                if not started:
                    pre_roll.append(mono)
                    if level > threshold:
                        started = True
                        started_at = last_partial = time.monotonic()
                        collected.extend(pre_roll)
                        if on_speech_start:
                            on_speech_start()
                    continue
                collected.append(mono)
                silence = silence + BLOCK if level <= threshold else 0.0
                if (on_partial and cfg.LIVE_TEXT and time.monotonic() - last_partial >= PARTIAL_EVERY
                        and len(collected) * BLOCK > 0.9):
                    last_partial = time.monotonic()
                    buffer = np.concatenate(collected)
                    if (len(buffer) < cfg.SAMPLE_RATE * 10
                            and not self._final_transcribing.is_set()
                            and self._partial_lock.acquire(blocking=False)):
                        threading.Thread(target=self._partial_worker,
                                         args=(buffer.copy(), on_partial, recording), daemon=True).start()
                if silence >= cfg.SILENCE_TAIL or time.monotonic() - started_at >= cfg.MAX_PHRASE:
                    break
        audio = np.concatenate(collected) if collected else np.zeros(0, dtype=np.float32)
        return None if len(audio) < cfg.SAMPLE_RATE * MIN_SPEECH else audio

    def _partial_worker(self, buffer, on_partial, recording):
        try:
            if self._final_transcribing.is_set() or not recording.is_set():
                return
            if self._inference_lock.acquire(blocking=False):
                try:
                    text = self._transcribe(buffer, fast=True)
                    if text and recording.is_set():
                        on_partial(text)
                finally:
                    self._inference_lock.release()
        except Exception:
            pass
        finally:
            self._partial_lock.release()

    def transcribe(self, audio, fast=False):
        if audio is None or len(audio) == 0:
            return ""
        if not fast:
            self._final_transcribing.set()
        try:
            with self._inference_lock:
                text = self._transcribe(audio, fast)
            self.last_error = ""
            return text
        except Exception as exc:
            self.last_error = "Распознавание речи недоступно: %s" % exc
            raise RuntimeError(self.last_error) from exc
        finally:
            if not fast:
                self._final_transcribing.clear()

    def _transcribe(self, audio, fast=False):
        model = self.wake if fast else self.main
        segments, _info = model.transcribe(
            normalize_audio(audio), language="ru", beam_size=1 if fast else 5,
            vad_filter=True, condition_on_previous_text=False,
            no_speech_threshold=0.5, initial_prompt=None if fast else self.hint(),
        )
        self.last_stt = "local-fast" if fast else "local"
        return " ".join(segment.text.strip() for segment in segments).strip()

    def transcribe_active(self, audio):
        if audio is None or len(audio) == 0:
            return ""
        if cfg.SPEECH_MODE in ("auto", "cloud") and cfg.CLOUD_AUDIO and cfg.GROQ_KEY:
            try:
                from core import ai

                text = ai.transcribe(audio, language="ru")
                if text:
                    self.last_stt = "groq"
                    return text
            except Exception as exc:
                self._wake_error = "cloud STT fallback: %s" % exc
        return self.transcribe(audio, fast=False)

    def health(self):
        return {
            "device": self.device,
            "noise_floor": self.noise_floor,
            "threshold": self.threshold(),
            "wake_engine": self._selected_wake_engine or ("whisper" if cfg.WAKE_ENGINE == "auto" else cfg.WAKE_ENGINE),
            "wake_error": self._wake_error,
            "error": self.last_error,
            "last_stt": self.last_stt,
            "main_loaded": self._main is not None,
            "verifier_loaded": self._wake is not None,
        }

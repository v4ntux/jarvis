"""Voice on the server, free: faster-whisper for speech-to-text, Edge TTS for replies."""
import asyncio
import os
import re
import subprocess
import tempfile
import threading

import edge_tts
import numpy as np

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
TTS_VOICE = os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural")
TTS_FALLBACK_VOICE = "ru-RU-SvetlanaNeural"
TTS_RETRIES = ({}, {"rate": "+4%"}, {"volume": "+1%"}, {"rate": "+8%", "pitch": "-2Hz"}, {"rate": "-3%"}, {"rate": "+2%"})
# Whisper's stock phrases for silence and noise, learned from YouTube subtitles
HALLUCINATIONS = re.compile(r"субтитр|продолжение следует|спасибо за просмотр|подпишитесь|dimatorzok|"
                            r"редактор|корректор|amara\.org", re.I)

_model = None
_lock = threading.Lock()


def _recognize(audio: np.ndarray) -> str:
    global _model
    with _lock:
        if _model is None:
            from faster_whisper import WhisperModel

            _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
        segments, _ = _model.transcribe(audio, beam_size=1, vad_filter=True, language="ru")
        text = " ".join(s.text.strip() for s in segments).strip()
    return "" if HALLUCINATIONS.search(text) else text


def _transcribe(path: str) -> str:
    # Decode with ffmpeg ourselves: faster-whisper's PyAV decoder breaks on newer av releases
    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    return _recognize(np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0)


async def transcribe(path: str) -> str:
    return await asyncio.to_thread(_transcribe, path)


def to_16k(pcm: bytes, rate: int) -> np.ndarray:
    """16-bit mono PCM at 48/32/16 kHz → float samples at 16 kHz; averaging also filters what decimation would alias."""
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    step = rate // 16000
    if step > 1:
        audio = audio[: len(audio) // step * step].reshape(-1, step).mean(axis=1)
    return audio


async def transcribe_pcm(pcm: bytes, rate: int) -> str:
    return await asyncio.to_thread(_recognize, to_16k(pcm, rate))


def _speakable(text: str) -> str:
    text = re.sub(r"\n*\(\$\d+(\.\d+)?\)\s*$", "", text)  # the cost footer is not read aloud
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)  # code blocks are not read aloud
    text = re.sub(r"https?://\S+", "ссылка", text)
    text = re.sub(r"[*_`#>\[\]]", "", text)
    return text[:1500]


def sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?…])\s+|\n+", _speakable(text))
    return [p.strip() for p in parts if re.search(r"\w", p)]


async def _edge(text: str, voice: str, **prosody) -> bytes:
    audio = bytearray()
    async for chunk in edge_tts.Communicate(text, voice, **prosody).stream():
        if chunk["type"] == "audio":
            audio += chunk["data"]
    return bytes(audio)


async def _sentence_mp3(sentence: str) -> bytes:
    # Since October 2026 the Russian male voice returns no audio for some phrases, depending on
    # the exact text and prosody: a barely different tempo gets through, the female voice is the last resort
    for prosody in TTS_RETRIES:
        try:
            audio = await _edge(sentence, TTS_VOICE, **prosody)
            if audio:
                return audio
        except edge_tts.exceptions.NoAudioReceived:
            pass
    return await _edge(sentence, TTS_FALLBACK_VOICE)


async def _mp3(text: str) -> bytes:
    """Edge TTS one sentence per request, all sentences at once."""
    audio = b"".join(await asyncio.gather(*(_sentence_mp3(s) for s in sentences(text))))
    if not audio:
        raise RuntimeError("Edge TTS вернул пустой звук")
    return audio


async def _ffmpeg(mp3: bytes, *output: str) -> bytes:
    proc = await asyncio.create_subprocess_exec("ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", *output,
                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate(mp3)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg failed")
    return out


async def synthesize(text: str) -> str:
    """Returns a path to an OGG/Opus file that Telegram accepts as a voice message."""
    fd, ogg = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    try:
        await _ffmpeg(await _mp3(text), "-c:a", "libopus", "-b:a", "32k", ogg)
    except BaseException:
        os.unlink(ogg)
        raise
    return ogg


async def synthesize_pcm(text: str, rate: int) -> bytes:
    """Speech as raw 16-bit mono PCM, for playing straight into a call."""
    return await _ffmpeg(await _mp3(text), "-f", "s16le", "-ac", "1", "-ar", str(rate), "-")

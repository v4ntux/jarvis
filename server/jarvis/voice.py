"""Voice on the server, free: faster-whisper for speech-to-text, Edge TTS for replies."""
import asyncio
import os
import re
import tempfile

import edge_tts

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
TTS_VOICE = os.environ.get("TTS_VOICE", "ru-RU-DmitryNeural")

_model = None


def _transcribe(path: str) -> str:
    global _model
    if _model is None:
        from faster_whisper import WhisperModel

        _model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    # Decode with ffmpeg ourselves: faster-whisper's PyAV decoder breaks on newer av releases
    import subprocess

    import numpy as np

    pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                         capture_output=True, check=True).stdout
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    segments, _ = _model.transcribe(audio, beam_size=1, vad_filter=True, language="ru")
    return " ".join(s.text.strip() for s in segments).strip()


async def transcribe(path: str) -> str:
    return await asyncio.to_thread(_transcribe, path)


def _speakable(text: str) -> str:
    text = re.sub(r"`{3}.*?`{3}", " ", text, flags=re.S)  # code blocks are not read aloud
    text = re.sub(r"https?://\S+", "ссылка", text)
    text = re.sub(r"[*_`#>\[\]]", "", text)
    return text[:1500]


async def synthesize(text: str) -> str:
    """Returns a path to an OGG/Opus file that Telegram accepts as a voice message."""
    fd, mp3 = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    ogg = mp3[:-4] + ".ogg"
    try:
        await edge_tts.Communicate(_speakable(text), TTS_VOICE).save(mp3)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-i", mp3, "-c:a", "libopus", "-b:a", "32k", ogg)
        if await proc.wait() != 0:
            raise RuntimeError("ffmpeg failed")
        return ogg
    finally:
        os.unlink(mp3)

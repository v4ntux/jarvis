"""Audio pipeline checks use generated samples/fake streams, never user audio."""
import asyncio
import threading
import time
import types
import unittest
from unittest import mock

import numpy as np

from core import ears, voice
from core import prepare_voice


class FakeInput:
    def __init__(self, blocks=(), **kwargs):
        self.blocks = blocks
        self.callback = kwargs["callback"]

    def __enter__(self):
        for block in self.blocks:
            self.callback(block.reshape(-1, 1), len(block), None, None)
        return self

    def __exit__(self, *_):
        pass


class RecordingTests(unittest.TestCase):
    def test_refresh_applies_microphone_and_unloads_changed_model_without_loading(self):
        listener = ears.Ears()
        listener._main = object()
        listener._main_name = "tiny"
        with mock.patch.object(ears.cfg, "MIC", "7"), \
                mock.patch.object(ears.cfg, "MODEL_MAIN", "base"), \
                mock.patch.object(ears, "sd") as device, \
                mock.patch.object(listener, "_load") as load:
            self.assertTrue(listener.refresh_settings())
        self.assertEqual(listener.device, 7)
        self.assertIsNone(listener._main)
        device.check_input_settings.assert_called_once_with(device=7, samplerate=16000,
                                                            channels=1, dtype="float32")
        device.InputStream.assert_not_called()
        load.assert_not_called()

    def test_invalid_microphone_stays_selected_and_reports_error(self):
        listener = ears.Ears()
        listener.device = 1
        with mock.patch.object(ears.cfg, "MIC", "999"), mock.patch.object(ears, "sd") as device:
            device.check_input_settings.side_effect = ValueError("device does not exist")
            self.assertFalse(listener.refresh_settings())
        self.assertEqual(listener.device, 999)
        self.assertIn("device does not exist", listener.last_error)

    def test_refresh_keeps_unchanged_loaded_model(self):
        listener = ears.Ears()
        model = object()
        listener._main = model
        listener._main_name = ears.cfg.MODEL_MAIN
        with mock.patch.object(ears, "sd"):
            self.assertTrue(listener.refresh_settings())
        self.assertIs(listener._main, model)

    def test_cached_model_load_never_checks_network(self):
        with mock.patch("faster_whisper.WhisperModel") as model:
            ears.Ears()._load("tiny")
        model.assert_called_once_with("tiny", local_files_only=True, device="cpu",
                                      compute_type="int8", cpu_threads=2)

    def test_missing_model_fails_with_explicit_setup_command(self):
        from huggingface_hub.errors import LocalEntryNotFoundError
        with mock.patch("faster_whisper.WhisperModel", side_effect=LocalEntryNotFoundError("missing")):
            with self.assertRaisesRegex(RuntimeError, "python -m core.prepare_voice"):
                ears.Ears()._load("tiny")

    def test_text_construction_never_queries_microphone(self):
        with mock.patch.object(ears, "sd", None), mock.patch.object(ears.cfg, "MIC", "5"):
            listener = ears.Ears()
        self.assertEqual(listener.device, 5)
        self.assertIsNone(listener._main)

    def test_auto_wake_uses_russian_local_transcription(self):
        listener = ears.Ears()
        with mock.patch.object(ears.cfg, "WAKE_ENGINE", "auto"), \
                mock.patch.object(listener, "_wait_whisper_candidate", return_value="candidate") as local, \
                mock.patch.object(listener, "_wait_openwakeword") as english:
            self.assertEqual(listener.wait_for_wake(), "candidate")
        local.assert_called_once()
        english.assert_not_called()
        self.assertEqual(listener.health()["wake_engine"], "whisper")

    def test_explicit_detector_error_reaches_runtime(self):
        listener = ears.Ears()
        with mock.patch.object(ears.cfg, "WAKE_ENGINE", "openwakeword"), \
                mock.patch.object(listener, "_wait_openwakeword", side_effect=OSError("model missing")):
            with self.assertRaisesRegex(RuntimeError, "model missing"):
                listener.wait_for_wake()
        self.assertIn("model missing", listener.health()["error"])

    def test_already_cancelled_does_not_open_microphone(self):
        listener = ears.Ears()
        with mock.patch.object(listener, "_input_stream") as stream:
            self.assertIsNone(listener.record(stop_flag=lambda: True))
            self.assertIsNone(listener.wait_for_wake(stop_flag=lambda: True))
            self.assertEqual(listener.calibrate(stop_flag=lambda: True), listener.noise_floor)
        stream.assert_not_called()

    def test_calibration_uses_owned_stream_and_computes_noise_floor(self):
        listener = ears.Ears()
        count = int(ears.cfg.SAMPLE_RATE * ears.BLOCK)
        blocks = [np.full(count, 0.002, dtype=np.float32) for _ in range(3)]
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(blocks, **kw)), \
                mock.patch.object(ears, "sd") as device:
            self.assertAlmostEqual(listener.calibrate(0.05), 0.002, places=5)
        device.rec.assert_not_called()
        device.wait.assert_not_called()

    def test_calibration_stalled_input_is_bounded(self):
        listener = ears.Ears()
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(**kw)):
            started = time.monotonic()
            listener.calibrate(0.05)
        self.assertLess(time.monotonic() - started, 1.4)
        self.assertIn("калибровки", listener.last_error)

    def test_calibration_cancels_while_waiting(self):
        listener = ears.Ears()
        stopped = threading.Event()
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(**kw)):
            worker = threading.Thread(target=lambda: listener.calibrate(stop_flag=stopped.is_set))
            worker.start()
            stopped.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())

    def test_pre_roll_retains_quiet_consonants_before_speech(self):
        listener = ears.Ears()
        count = int(ears.cfg.SAMPLE_RATE * ears.BLOCK)
        prefix = [np.full(count, 0.001, dtype=np.float32) for _ in range(4)]
        speech = [np.full(count, 0.2, dtype=np.float32) for _ in range(10)]
        silence = [np.zeros(count, dtype=np.float32) for _ in range(4)]
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(prefix + speech + silence, **kw)), \
                mock.patch.object(ears.cfg, "SILENCE_TAIL", 0.09):
            audio = listener.record(max_wait=1)
        self.assertIsNotNone(audio)
        np.testing.assert_allclose(audio[:count * 4], 0.001)
        self.assertGreater(float(audio[count * 4]), 0.1)

    def test_empty_stream_respects_wall_clock_timeout(self):
        listener = ears.Ears()
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(**kw)):
            started = time.monotonic()
            self.assertIsNone(listener.record(max_wait=0.12))
        self.assertLess(time.monotonic() - started, 0.6)

    def test_disconnected_idle_microphone_surfaces_error(self):
        listener = ears.Ears()
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(**kw)), \
                mock.patch.object(ears, "INPUT_STALL_TIMEOUT", 0.01):
            with self.assertRaisesRegex(RuntimeError, "Нет аудиоданных"):
                listener.record()
        self.assertTrue(listener.last_error)

    def test_cancel_interrupts_empty_stream(self):
        listener = ears.Ears()
        stopped = threading.Event()
        result = []
        with mock.patch.object(listener, "_input_stream", side_effect=lambda **kw: FakeInput(**kw)):
            worker = threading.Thread(target=lambda: result.append(listener.record(stop_flag=stopped.is_set)))
            worker.start()
            stopped.set()
            worker.join(0.5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [None])

    def test_finished_partial_cannot_overwrite_final_text(self):
        listener = ears.Ears()
        recording = threading.Event()
        callback = mock.Mock()
        listener._partial_lock.acquire()
        with mock.patch.object(listener, "_transcribe") as transcribe:
            listener._partial_worker(np.ones(100), callback, recording)
        transcribe.assert_not_called()
        callback.assert_not_called()
        self.assertFalse(listener._partial_lock.locked())

    def test_partial_skips_when_final_transcription_is_waiting(self):
        listener = ears.Ears()
        listener._final_transcribing.set()
        recording = threading.Event()
        recording.set()
        listener._partial_lock.acquire()
        with mock.patch.object(listener, "_transcribe") as transcribe:
            listener._partial_worker(np.ones(100), mock.Mock(), recording)
        transcribe.assert_not_called()
        self.assertFalse(listener._partial_lock.locked())


class VoiceTests(unittest.TestCase):
    def setUp(self):
        self.speaker = voice.Voice()
        self.speaker.enabled = True

    def test_refresh_applies_voice_and_tts_toggle_without_audio(self):
        with mock.patch.object(voice.cfg, "TTS_ENABLED", False), \
                mock.patch.object(voice.cfg, "VOICE", "ru-RU-SvetlanaNeural"), \
                mock.patch.object(voice.cfg, "VOICE_RATE", "+20%"), \
                mock.patch.object(voice.cfg, "VOICE_PITCH", "-10Hz"), \
                mock.patch.object(self.speaker, "_pcm_for") as synth, \
                mock.patch.object(voice, "sd") as device:
            self.assertTrue(self.speaker.refresh_settings())
        self.assertFalse(self.speaker.enabled)
        self.assertEqual(self.speaker.name, "ru-RU-SvetlanaNeural")
        self.assertEqual((self.speaker.rate, self.speaker.pitch), ("+20%", "-10Hz"))
        self.assertEqual(self.speaker.last_backend, "disabled")
        synth.assert_not_called()
        device.OutputStream.assert_not_called()

    def test_refresh_rejects_invalid_voice_parameters_explicitly(self):
        with mock.patch.object(voice.cfg, "VOICE_RATE", "fast"):
            self.assertFalse(self.speaker.refresh_settings())
        self.assertEqual(self.speaker.rate, "fast")
        self.assertIn("параметры", self.speaker.last_error)

    def test_success_is_reported_only_after_playback(self):
        with mock.patch.object(self.speaker, "_pcm_for", return_value=np.ones(5, dtype=np.int16)), \
                mock.patch.object(self.speaker, "_play") as play:
            self.assertTrue(self.speaker.say("Проверка"))
        play.assert_called_once()
        self.assertTrue(self.speaker.last_success)
        self.assertEqual(self.speaker.last_backend, "edge")
        self.assertFalse(self.speaker.speaking)

    def test_both_backend_failures_are_observable(self):
        with mock.patch.object(self.speaker, "_pcm_for", side_effect=OSError("network down")), \
                mock.patch.object(self.speaker, "_sapi", side_effect=RuntimeError("no Russian voice")):
            self.assertFalse(self.speaker.say("Проверка"))
        self.assertIn("network down", self.speaker.last_error)
        self.assertIn("no Russian voice", self.speaker.last_error)
        self.assertEqual(self.speaker.last_backend, "unavailable")
        self.assertFalse(self.speaker.speaking)

    def test_offline_fallback_reports_backend(self):
        with mock.patch.object(self.speaker, "_pcm_for", side_effect=OSError("network down")), \
                mock.patch.object(self.speaker, "_sapi") as fallback:
            self.assertTrue(self.speaker.say("Проверка"))
        fallback.assert_called_once()
        self.assertEqual(self.speaker.last_backend, "sapi")

    def test_cancelled_synthesis_never_plays_or_falls_back(self):
        def synth(*_args, **_kwargs):
            self.speaker.stop()
            return np.ones(5, dtype=np.int16)
        with mock.patch.object(self.speaker, "_pcm_for", side_effect=synth), \
                mock.patch.object(self.speaker, "_play") as play, \
                mock.patch.object(self.speaker, "_sapi") as fallback:
            self.assertFalse(self.speaker.say("Проверка"))
        play.assert_not_called()
        fallback.assert_not_called()
        self.assertEqual(self.speaker.last_backend, "stopped")

    def test_stop_does_not_use_global_sounddevice_stop(self):
        output = mock.Mock()
        self.speaker._output = output
        with mock.patch.object(voice, "sd") as device:
            self.speaker.stop()
        output.abort.assert_called_once()
        device.stop.assert_not_called()

    def test_old_queued_utterance_is_discarded_after_stop(self):
        generation = self.speaker._generation
        self.speaker.stop()
        with mock.patch.object(self.speaker, "_pcm_for") as synth:
            self.assertFalse(self.speaker._say("старый ответ", generation))
        synth.assert_not_called()

    def test_synthesis_has_total_deadline(self):
        async def hung(_):
            await asyncio.sleep(10)
        with mock.patch.object(self.speaker, "_synthesize", side_effect=hung), \
                mock.patch.object(voice, "SYNTHESIS_TIMEOUT", 0.02):
            with self.assertRaises(TimeoutError):
                asyncio.run(self.speaker._synthesize_bounded("Проверка"))

    def test_synthesis_cancels_without_waiting_for_network(self):
        async def hung(_):
            await asyncio.sleep(10)
        with mock.patch.object(self.speaker, "_synthesize", side_effect=hung):
            with self.assertRaises(voice.SpeechCancelled):
                asyncio.run(self.speaker._synthesize_bounded("Проверка", cancelled=lambda: True))

    def test_sapi_initializes_and_releases_com_on_worker(self):
        fake_com = mock.Mock()
        fake_client = mock.Mock()
        fake_speaker = fake_client.Dispatch.return_value
        fake_speaker.WaitUntilDone.return_value = True
        fake_package = types.ModuleType("win32com")
        fake_package.client = fake_client
        with mock.patch.dict("sys.modules", {"pythoncom": fake_com, "win32com": fake_package,
                                              "win32com.client": fake_client}):
            self.speaker._sapi("Hello", lambda: False)
        fake_com.CoInitialize.assert_called_once()
        fake_com.CoUninitialize.assert_called_once()
        fake_speaker.Speak.assert_called_once_with("Hello", 1)


class PrepareVoiceTests(unittest.TestCase):
    def test_check_never_downloads_and_deduplicates_models(self):
        with mock.patch.object(prepare_voice.cfg, "MODEL_WAKE", "base"), \
                mock.patch.object(prepare_voice.cfg, "MODEL_MAIN", "base"), \
                mock.patch("faster_whisper.utils.download_model") as download:
            self.assertEqual(prepare_voice.prepare(check_only=True), 0)
        download.assert_called_once_with("base", local_files_only=True)

    def test_install_downloads_each_configured_model(self):
        with mock.patch.object(prepare_voice.cfg, "MODEL_WAKE", "tiny"), \
                mock.patch.object(prepare_voice.cfg, "MODEL_MAIN", "base"), \
                mock.patch("faster_whisper.utils.download_model") as download:
            self.assertEqual(prepare_voice.prepare(), 0)
        self.assertEqual(download.call_args_list, [mock.call("tiny", local_files_only=False),
                                                  mock.call("base", local_files_only=False)])


if __name__ == "__main__":
    unittest.main()

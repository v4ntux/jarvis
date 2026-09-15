"""Джарвис — голосовой помощник. Точка входа.

По умолчанию просто запускается и живёт плашкой сверху экрана: слушает,
понимает команды локальными правилами и отвечает голосом. Интернет и платные
модели не нужны.

Ключи запуска:
  --console        работать в консоли, без плашки (для отладки)
  --say "текст"    выполнить одну команду и выйти
  --ui             посмотреть, как выглядит плашка
  --check          прогнать проверки микрофона, голоса и мозга
  --autostart on   запускаться вместе с Windows (off — отключить)
  --mics           список микрофонов
"""
import os
import sys
import threading
import time

# pythonw and the packaged app both need a persistent error log.
if getattr(sys, "frozen", False) or sys.stdout is None:
    try:
        _data = os.path.join(os.environ.get("LOCALAPPDATA", os.path.dirname(sys.executable)), "Jarvis")
        os.makedirs(_data, exist_ok=True)
        _log = open(os.path.join(_data, "jarvis.log"),
                    "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log
    except Exception:
        pass

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from core import ai, ears as ears_module, router, settings as cfg  # noqa: E402
from core.session import Session  # noqa: E402
from core.voice import Voice  # noqa: E402

# Фразы, которые звучат чаще всего — синтезируем заранее, чтобы отвечал мгновенно.
COMMON_PHRASES = ("Да, %s?" % cfg.TITLE, "Готово.", "Слушаю.", "Прибавил.", "Убавил.",
                  "Открываю.", "Свернул.", "Ищу.", "Отменил.", "Хорошо, зовите.",
                  "Не понял команду. Скажите иначе или включите внешний мозг в настройках.")


def log(message):
    print(message, flush=True)


_instance_mutex = None
_show_event = None


def acquire_single_instance():
    """Keep the normal desktop app single-instance without affecting CLI tools."""
    global _instance_mutex, _show_event
    if sys.platform != "win32":
        return True
    import ctypes

    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.SetEvent.argtypes = [wintypes.HANDLE]
    _instance_mutex = kernel.CreateMutexW(None, False, "Local\\JarvisSilentCoreBeta")
    already_running = ctypes.get_last_error() == 183
    _show_event = kernel.CreateEventW(None, False, False, "Local\\JarvisDesktopShow")
    if already_running and _show_event:
        kernel.SetEvent(_show_event)
    return not already_running


# --- проверки для панели ----------------------------------------------------
def make_tests(ears, voice, stop_flag=None):
    def test_mic():
        ears.calibrate(0.8, stop_flag=stop_flag)
        audio = ears.record(max_wait=6.0, stop_flag=stop_flag)
        if audio is None:
            return "Речь не поймана. Проверьте микрофон и чувствительность."
        peak = float(max(abs(audio.min()), abs(audio.max())))
        text = ears.transcribe(audio)
        note = ""
        if peak < 0.05:
            note = "\nСигнал тихий — прибавьте усиление микрофона в Windows."
        elif peak > 0.98:
            note = "\nСигнал перегружен — убавьте усиление."
        return "Услышал: «%s»\nГромкость сигнала: %.2f, фон: %.4f%s" % (
            text or "(пусто)", peak, ears.noise_floor, note)

    def test_voice():
        if not voice.say("Проверка связи. Джарвис на связи, %s." % cfg.TITLE):
            return "Голос не воспроизведён: %s" % (voice.last_error or "озвучка выключена")
        return "Фраза произнесена голосом %s.\nКеш фраз: %d КБ." % (
            voice.name, voice.cache_size() // 1024)

    def test_brain():
        ok, message = ai.check()
        return ("Работает: " if ok else "Не работает: ") + message

    def test_wake():
        deadline = time.time() + 8.0
        event = ears.wait_for_wake(stop_flag=lambda: time.time() >= deadline or bool(stop_flag and stop_flag()))
        if event is None:
            return "Wake не пойман за 8 секунд. Скажите «Джарвис» рядом с микрофоном."
        draft = ears.transcribe(event.audio, fast=True)
        called, command = ears_module.split_wake(draft)
        return ("Wake подтверждён локально.\nEngine: %s\nScore: %.3f\nТекст: %s\nКоманда: %s"
                % (event.engine, event.score, draft or "(пусто)", command or "(нет)")) \
            if called else "Кандидат был, но имя не подтвердилось: %s" % (draft or "(пусто)")

    def test_ready():
        from core import memory, skills

        info = memory.database_info()
        providers = ai.chain()
        lines = [
            "JARVIS 2.0 — СОСТОЯНИЕ",
            "микрофоны: %d" % len(ears_module.Ears.devices()),
            "wake engine: %s" % cfg.WAKE_ENGINE,
            "speech mode: %s" % cfg.SPEECH_MODE,
            "локальных skills: %d" % len(skills.SKILLS),
            "SQLite: %s (%d bytes)" % (info["path"], info["bytes"]),
            "AI Pool: %s" % (", ".join(providers) or "local only"),
            "настройки: %s" % cfg.ENV_PATH,
            "чат: доступен независимо от микрофона",
            "активация: имя / кнопка / Ctrl+Alt+Space",
            "секреты: Windows DPAPI",
            "запись микрофона на диск: нет",
        ]
        return "\n".join(lines)

    def test_devices():
        lines = ["%2d  %s" % (index, name) for index, name in ears_module.Ears.devices()]
        return "Микрофоны (номер вписывается в настройках):\n" + "\n".join(lines)

    test_mic.uses_microphone = True
    test_wake.uses_microphone = True
    return {"mic": test_mic, "voice": test_voice, "brain": test_brain,
            "devices": test_devices, "wake": test_wake, "ready": test_ready}


# --- режимы -----------------------------------------------------------------
def run_console(ears, voice):
    """Текстовый режим: печатаешь команду, пустой Enter — сказать голосом."""
    session = Session(ears, voice, ui=None, log=log)
    session.wake_up()
    print("Локальных правил: %d. Внешний мозг: %s." % (router.rules_count(),
                                                       ", ".join(ai.chain()) or "выключен"))
    print("Пустой Enter — говорить голосом, «выход» — закончить.\n")
    while True:
        try:
            text = input("Вы: ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if text.lower() in ("выход", "exit", "quit"):
            return
        if not text:
            print("  [слушаю...]", flush=True)
            audio = ears.record(max_wait=8.0)
            if audio is None:
                print("  [тишина]")
                continue
            text = ears.transcribe(audio)
            print("Вы (голос): %s" % text)
        session.handle(text)


def run_island(ears, voice, start_paused=False):
    """Desktop shell stays alive when audio or an AI provider is unavailable."""
    from ui.island import Island, UiState
    from core.runtime import AssistantRuntime
    from core.hotkeys import GlobalHotkeys
    from core.tasks import TaskManager

    state = UiState()
    session = Session(ears, voice, ui=state, log=log, require_wake=True)
    runtime = AssistantRuntime(session, state, log=log)
    tasks = TaskManager(on_change=lambda items: state.set(tasks=items),
                        on_message=runtime.task_notification)
    runtime.task_manager = tasks
    runtime.set_paused(start_paused)
    island = Island(state, on_quit=runtime.stop, on_toggle_pause=runtime.set_paused,
                    on_test=make_tests(ears, voice, lambda: runtime.stopped.is_set() or
                                      runtime.cancelled.is_set() or runtime.paused.is_set()),
                    session=session, runtime=runtime)
    runtime.target_provider = lambda: island._external_window
    hotkeys = GlobalHotkeys(
        lambda: island.call_soon(island._listen),
        lambda: island.call_soon(island.show_chat),
        runtime.cancel,
        lambda message: state.set(error=message),
    )

    from core.scheduler import ReminderScheduler

    def reminder_due(reminder):
        state.add_message("assistant", "Напоминание: %s" % reminder["text"])
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass
        snapshot = state.snapshot()
        if snapshot["status"] in ("idle", "reminder"):
            state.set(status="reminder", reminder=reminder,
                      reply="Напоминание: %s" % reminder["text"])
            def clear_reminder():
                current = state.snapshot()
                if (current.get("status") == "reminder"
                        and (current.get("reminder") or {}).get("id") == reminder["id"]):
                    state.set(status="idle", reminder=None, reply="")
            threading.Timer(12.0, clear_reminder).start()

    scheduler = ReminderScheduler(reminder_due)
    scheduler.start()

    tasks.start()
    runtime.start()
    hotkeys.start()
    island.root.after(100, island.show_chat)
    if _show_event:
        import ctypes
        from ctypes import wintypes
        wait_event = ctypes.windll.kernel32.WaitForSingleObject
        wait_event.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        wait_event.restype = wintypes.DWORD

        def check_show_request():
            if island._closing:
                return
            if wait_event(_show_event, 0) == 0:
                island.show_chat()
            island.root.after(300, check_show_request)
        island.root.after(300, check_show_request)
    if not cfg.FIRST_RUN_MARK.exists():
        island.root.after(500, island.show_onboarding)
    try:
        island.run()
    finally:
        runtime.stop()
        hotkeys.stop()
        scheduler.stop()


def main():
    args = sys.argv[1:]
    flags = [a for a in args if a.startswith("--")]

    cli_mode = any(flag != "--text-only" for flag in flags)
    if not cli_mode and not acquire_single_instance():
        return

    if "--mics" in flags:
        for index, name in ears_module.Ears.devices():
            print("%2d  %s" % (index, name))
        return

    if "--ui" in flags:
        from ui import island

        island.preview(20)
        return

    if "--prepare-voice" in flags:
        from core.prepare_voice import prepare
        raise SystemExit(prepare())

    if "--autostart" in flags:
        from core import autostart

        position = args.index("--autostart")
        value = args[position + 1].lower() if len(args) > position + 1 else "status"
        if value in ("on", "вкл", "1"):
            print("Готово:", autostart.enable())
        elif value in ("off", "выкл", "0"):
            print("Автозапуск убран." if autostart.disable() else "Он и не был включён.")
        else:
            print(autostart.status_line())
        return

    ears = ears_module.Ears()
    voice = Voice()

    if "--check" in flags:
        tests = make_tests(ears, voice)
        for key in ("ready", "devices", "brain"):
            print("\n=== %s ===" % key)
            print(tests[key]())
        return

    if "--say" in flags:
        text = " ".join(a for a in args if not a.startswith("--")).strip()
        session = Session(ears, voice, ui=None, log=log)
        session.wake_up()
        session.handle(text)
        return

    if "--console" in flags:
        run_console(ears, voice)
        return

    run_island(ears, voice, start_paused="--text-only" in flags)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        import traceback

        traceback.print_exc()
        if getattr(sys, "frozen", False):
            sys.exit(1)
        raise

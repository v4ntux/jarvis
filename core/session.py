"""Strict conversation state machine and safe execution orchestration."""
import time
import uuid
import re
from contextlib import nullcontext

from core import actions, ai, desktop_lock, ears as ears_module, memory, router, settings as cfg, skills


SLEEP_MARK = "__SLEEP__"
CONFIRM_MARK = "__CONFIRM__"
DICTATION_START_MARK = "__DICTATION_START__"
DICTATION_STOP_MARK = "__DICTATION_STOP__"
CLIPBOARD_TRANSFORM_MARK = "__CLIPBOARD_TRANSFORM__"
CLIPBOARD_RESTORE_MARK = "__CLIPBOARD_RESTORE__"


class ConversationEndDetector:
    """Deterministic end scoring; intentionally makes no AI request."""

    EXPLICIT = (
        "отбой", "свободен", "это все", "это всё", "спасибо все", "спасибо всё",
        "можешь спать", "хватит", "закончили",
    )

    @classmethod
    def explicit(cls, text):
        normalized = router.normalize(text)
        return normalized in cls.EXPLICIT or normalized == "спасибо это все"

    @staticmethod
    def score(silence_seconds, pending=False, last_reply_question=False,
              action_complete=True, user_speaking=False):
        if user_speaking:
            return -100
        score = 20 if action_complete else 0
        score += 20 if not pending else -70
        if silence_seconds >= 8:
            score += 35
        if silence_seconds >= 15:
            score += 25
        if last_reply_question:
            score -= 50
        return score


class Session:
    def __init__(self, ears, voice, ui=None, log=print, require_wake=False, cancel_flag=None):
        self.ears = ears
        self.voice = voice
        self.ui = ui
        self.log = log
        self.require_wake = bool(require_wake)
        self.cancel_flag = cancel_flag or (lambda: False)
        self._remaining_steps = []
        self._plan_total = 0
        self._plan_index = 0
        self.active_until = 0.0
        self.last_activity = 0.0
        self.session_id = ""
        self.history = []
        self.pending = None
        self.dictation = False
        self._clipboard_backup = None
        self.last_reply_question = False
        self.state = "idle"
        self.stats = {"rules": 0, "learned": 0, "ai": 0, "unknown": 0,
                      "wake_candidates": 0, "wake_verified": 0, "cloud_stt": 0}

    def _ui(self, **fields):
        if "status" in fields:
            self.state = fields["status"]
        if self.ui is not None:
            fields.setdefault("session_id", self.session_id)
            self.ui.set(**fields)

    def _record(self, role, text):
        if not text:
            return
        self.history = (self.history + [(role, str(text)[:12000])])[-24:]
        if self.ui is not None and hasattr(self.ui, "add_message"):
            self.ui.add_message(role, str(text))

    def _cancelled(self):
        if self.cancel_flag():
            self._remaining_steps = []
            self.pending = None
            self._ui(status="idle", progress="Остановлено", action="", confirmation=None)
            return True
        return False

    def _say(self, text, status="speaking"):
        if not text or self._cancelled():
            return
        text = str(text)
        self.last_reply_question = text.rstrip().endswith("?")
        self._record("assistant", text)
        self._ui(status=status, reply=text)
        self.log("%s: %s" % (cfg.NAME, text))
        self.voice.say(text)
        voice_error = getattr(self.voice, "last_error", "")
        if isinstance(voice_error, str) and voice_error:
            self._ui(error=voice_error)

    @property
    def active(self):
        alive = time.time() < self.active_until
        return alive and (bool(self.session_id) or not self.require_wake)

    def wake_up(self, reason="manual"):
        self.session_id = uuid.uuid4().hex
        self.last_activity = time.time()
        self.active_until = self.last_activity + max(cfg.FOLLOW_UP, 5)
        self.stats["wake_verified"] += int(reason != "manual")
        self._ui(status="listening", heard="", reply="", action="", wake_reason=reason)
        return self.session_id

    def extend(self, kind="normal"):
        windows = {
            "action": cfg.FOLLOW_ACTION,
            "question": cfg.FOLLOW_QUESTION,
            "confirmation": cfg.FOLLOW_QUESTION,
            "normal": cfg.FOLLOW_UP,
        }
        seconds = max(0, int(windows.get(kind, cfg.FOLLOW_UP)))
        self.last_activity = time.time()
        if seconds > 0:
            self.active_until = self.last_activity + seconds

    def should_sleep(self, now=None):
        moment = time.time() if now is None else float(now)
        if self.pending and moment < self.pending["expires_at"]:
            return False
        silence = moment - self.last_activity
        score = ConversationEndDetector.score(
            silence, pending=bool(self.pending),
            last_reply_question=self.last_reply_question, action_complete=True,
        )
        return moment >= self.active_until or score >= 70

    def go_to_sleep(self, reason="timeout"):
        old_session = self.session_id
        self.active_until = 0.0
        self.last_activity = 0.0
        self.session_id = ""
        self.pending = None
        self.dictation = False
        if reason in ("new_conversation", "reset"):
            self.history = []
        self._remaining_steps = []
        self.last_reply_question = False
        self._ui(status="idle", heard="", reply="", action="", confirmation=None, end_reason=reason,
                 previous_session=old_session)

    def handle(self, text):
        text = (text or "").strip()
        if not text or self._cancelled():
            return False
        if self.require_wake and not self.active:
            self.log("  [blocked without wake]")
            return False
        self.last_activity = time.time()
        self._record("user", text)
        self._ui(heard=text, error="")
        if self.pending is not None:
            self._resolve_confirmation(text)
            return True
        if router.normalize(text) in ("новый разговор", "начни новый разговор", "забудь этот разговор"):
            self.history = []
            self._say("Начинаем новый разговор. Слушаю.")
            self.extend("question")
            return True
        if ConversationEndDetector.explicit(text):
            self._say("Хорошо, зовите.")
            self.go_to_sleep("explicit")
            return True
        if self.dictation:
            self._handle_dictation(text)
            return True

        self._ui(status="thinking", action="", progress="Разбираю запрос")
        # Learned complete phrases retain explicitly chosen app aliases. Never
        # reuse a contextual 'это/его/туда' instruction as a static command.
        learned, params = memory.lookup(text, exact=True)
        if learned and learned not in ("type_text", "add_note", "web_search", "add_reminder"):
            self.stats["learned"] += 1
            self._run_skill(learned, params)
            return True
        result = router.route(text, title=cfg.TITLE, safe=True, execute=False)
        if result.handled:
            self.stats["rules"] += 1
            self._deliver_rule(result)
            return True
        if ai.enabled() and ai.chain():
            self._ask_ai(text)
            return True
        self.stats["unknown"] += 1
        self._say("Не понял команду. Сейчас доступны локальные команды: открыть программу, "
                  "поиск, громкость, ввод текста и таймер. Для разговора подключите AI в настройках.")
        self.extend("normal")
        return True

    def _deliver_rule(self, result):
        if result.steps:
            self._start_plan(result.steps)
            return
        reply = result.reply
        if reply == DICTATION_START_MARK:
            self.dictation = True
            self._say("Диктовка включена. Скажите «стоп диктовка», чтобы закончить.")
            self._ui(status="dictating", action="dictation")
            self.extend("question")
            return
        if reply == DICTATION_STOP_MARK:
            self.dictation = False
            self._say("Диктовка остановлена.")
            self.extend("action")
            return
        if reply == CLIPBOARD_TRANSFORM_MARK:
            self._transform_clipboard(result.params.get("instruction", ""))
            return
        if reply == CLIPBOARD_RESTORE_MARK:
            self._restore_clipboard()
            return
        if reply == SLEEP_MARK:
            self._say("Хорошо, зовите.")
            self.go_to_sleep("explicit")
            return
        if reply.startswith(CONFIRM_MARK):
            description, _, parsed_skill = reply[len(CONFIRM_MARK):].partition("|")
            skill_name = result.skill or parsed_skill
            self._request_confirmation(description, skill_name, result.params or {})
            return
        self._say(reply)
        if result.action and result.action not in ("greeting", "thanks", "ping"):
            try:
                memory.log_action(result.action, status="ok", message=reply,
                                  session_id=self.session_id)
            except Exception:
                pass
        self.extend("question" if self.last_reply_question else "action")

    @staticmethod
    def _dictation_render(text):
        rendered = str(text or "").strip()
        replacements = (
            (r"\bновый абзац\b", "\n\n"), (r"\bновая строка\b", "\n"),
            (r"\bвопросительный знак\b", "?"),
            (r"\bвосклицательный знак\b", "!"),
            (r"\bдвоеточие\b", ":"), (r"\bточка с запятой\b", ";"),
            (r"\bзапятая\b", ","), (r"\bточка\b", "."),
        )
        for pattern, value in replacements:
            rendered = re.sub(pattern, value, rendered, flags=re.I)
        rendered = re.sub(r" +([,.;:!?])", r"\1", rendered)
        rendered = re.sub(r"[ \t]*\n[ \t]*", "\n", rendered)
        return rendered

    def _handle_dictation(self, text):
        normalized = router.normalize(text)
        if re.match(r"^(стоп|останови|выключи|закончи)( режим)? диктовк", normalized):
            self.dictation = False
            self._say("Диктовка остановлена.")
            self.extend("action")
            return
        rendered = self._dictation_render(text)
        if not rendered:
            return
        try:
            result = skills.execute("type_text", {"text": rendered + ("" if rendered.endswith(("\n", " ")) else " ")})
            if not result.ok:
                raise actions.ActionError(result.message)
            memory.log_action("dictation", {"characters": len(rendered)}, "ok",
                              "text inserted", 1, self.session_id)
            self._ui(status="dictating", heard="", reply="", action="dictation")
            self.extend("question")
        except Exception as exc:
            self.dictation = False
            self._say("Диктовка остановлена: не удалось ввести текст.")
            self.log("  [dictation error: %s]" % type(exc).__name__)

    def _transform_clipboard(self, instruction):
        if not memory.permission("clipboard")["enabled"]:
            self._say("Работа с буфером отключена в настройках.")
            self.extend("normal")
            return
        try:
            with desktop_lock.acquire(blocking=False) as acquired:
                if not acquired:
                    self._say("Компьютер занят другой задачей. Повторите обработку буфера после её завершения.")
                    self.extend("normal")
                    return
                source = actions.clipboard_get()
        except Exception:
            self._say("Не удалось прочитать буфер обмена.")
            self.extend("normal")
            return
        if not source.strip():
            self._say("Буфер обмена пуст.")
            self.extend("action")
            return
        if not ai.enabled() or not ai.chain("chat"):
            self._say("Для обработки текста нужен настроенный AI Pool.")
            self.extend("action")
            return
        self._ui(status="thinking", action="text transform")
        try:
            transformed = ai.transform_text(source, instruction)
            if self._cancelled():
                return
            if not transformed.strip():
                raise ai.AiError("пустой ответ", "bad_response")
            with desktop_lock.acquire(blocking=False) as acquired:
                if not acquired:
                    self._say("Компьютер занят другой задачей. Буфер не изменял.")
                    self.extend("normal")
                    return
                if actions.clipboard_get() != source:
                    self._say("Буфер изменился во время обработки. Сохранил его текущий текст.")
                    self.extend("normal")
                    return
                actions.clipboard_set(transformed)
                self._clipboard_backup = source
            memory.log_action("clipboard_transform", {"characters": len(source)}, "ok",
                              "clipboard updated", 1, self.session_id)
            self._ui(provider=ai.last_provider(), provider_latency=ai.last_latency())
            self._say("Готово. Новый текст в буфере. Можно сказать «верни прошлый буфер».")
        except Exception as exc:
            self.log("  [clipboard transform error: %s]" % type(exc).__name__)
            self._say("Не удалось обработать текст через AI Pool.")
        self.extend("action")

    def _restore_clipboard(self):
        if self._clipboard_backup is None:
            self._say("Предыдущей версии буфера в этой сессии нет.")
        else:
            with desktop_lock.acquire(blocking=False) as acquired:
                if not acquired:
                    self._say("Компьютер занят другой задачей. Буфер не изменял.")
                else:
                    actions.clipboard_set(self._clipboard_backup)
                    self._clipboard_backup = None
                    self._say("Предыдущий буфер восстановлен.")
        self.extend("action")

    def _ask_ai(self, text):
        self._ui(status="thinking", action="Думаю", progress="Обращаюсь к AI")
        if self._cancelled():
            return
        try:
            resolution = ai.resolve(text, history=self.history[:-1])
        except ai.AiError as exc:
            self._say("AI сейчас недоступен: %s." % exc)
            self._ui(error=str(exc))
            self.extend("normal")
            return
        except Exception as exc:
            self.log("  [AI error: %s]" % type(exc).__name__)
            self._say("Не удалось получить ответ AI. Локальные команды продолжают работать.")
            self.extend("normal")
            return
        if self._cancelled():
            return
        self.stats["ai"] += 1
        self._ui(provider=ai.last_provider(), provider_latency=ai.last_latency())
        if isinstance(resolution, ai.Resolution):
            if resolution.delegate == "computer":
                dispatcher = getattr(self, "task_dispatch", None)
                self._say(dispatcher(text) if callable(dispatcher) else
                          "Для этой просьбы нужен режим управления компьютером. Откройте его в панели задач.")
                self.extend("normal")
                return
            steps, say = resolution.steps, resolution.say
        else:  # Older resolver integrations return a single skill triple.
            skill, params, say = resolution
            steps = [{"skill": skill, "params": params or {}}] if skill else []
        if steps:
            contextual = re.search(r"\b(это|его|ее|её|туда|там|также|снова|предыдущ\w*)\b", text, re.I)
            cacheable = len(steps) == 1 and steps[0]["skill"] not in ("type_text", "add_note", "web_search", "add_reminder")
            remember = text if cfg.LEARN and cacheable and not contextual else None
            self._start_plan(steps, remember_phrase=remember)
            return
        self._say(say or "Не знаю, что ответить.")
        self.extend("question" if self.last_reply_question else "normal")

    def _start_plan(self, steps, remember_phrase=None):
        if self._cancelled():
            return
        try:
            if not isinstance(steps, list) or not 1 <= len(steps) <= router.MAX_STEPS:
                raise ValueError("План должен содержать от одного до восьми шагов")
            clean = []
            for step in steps:
                if not isinstance(step, dict):
                    raise ValueError("Неверный шаг плана")
                name = step.get("skill")
                params = skills.validate_params(name, step.get("params", {}))
                if not memory.permission(name)["enabled"]:
                    raise ValueError("Умение «%s» отключено в настройках" % name)
                clean.append({"skill": name, "params": params})
        except (ValueError, TypeError) as exc:
            self._say("Не начал выполнение: %s." % exc)
            self._ui(error=str(exc), progress="Не выполнено")
            self.extend("normal")
            return
        self._remaining_steps = clean
        self._plan_total = len(clean)
        self._plan_index = 0
        self._continue_plan(remember_phrase)

    def _continue_plan(self, remember_phrase=None):
        while self._remaining_steps and not self._cancelled():
            step = self._remaining_steps.pop(0)
            self._plan_index += 1
            name, params = step["skill"], step["params"]
            self._ui(status="acting", action=skills.danger_text(name) or name,
                     progress="Шаг %d из %d" % (self._plan_index, self._plan_total))
            if not memory.permission(name)["enabled"]:
                self._remaining_steps = []
                self._say("Выполнение остановлено: это умение отключено.")
                break
            if skills.is_dangerous(name):
                description = skills.danger_text(name)
                target = params.get("name") or params.get("title") or params.get("keys") or ""
                if target:
                    description += " «%s»" % str(target)[:160]
                self._request_confirmation(description, name, params, remember_phrase)
                return
            if not self._perform(name, params, remember_phrase if self._plan_total == 1 else None):
                self._remaining_steps = []
                self._ui(progress="Остановлено на шаге %d" % self._plan_index)
                break
        if not self._cancelled():
            self._ui(status="listening", action="")
            self.extend("action")

    def _run_skill(self, skill, params, remember_phrase=None, spoken=""):
        self._start_plan([{"skill": skill, "params": params or {}}], remember_phrase)

    def _request_confirmation(self, description, skill, params,
                              remember_phrase=None, spoken=""):
        if not skill or not skills.exists(skill):
            self._say("Не могу безопасно подтвердить это действие.")
            self._remaining_steps = []
            self.extend("normal")
            return
        if not cfg.CONFIRM and skills.risk(skill) < 3 and not memory.permission(skill)["always_confirm"]:
            if self._perform(skill, params, remember_phrase):
                self._continue_plan()
            else:
                self._remaining_steps = []
                self.extend("action")
            return
        self.pending = {
            "id": uuid.uuid4().hex,
            "description": description,
            "skill": skill,
            "params": params or {},
            "remember_phrase": remember_phrase,
            "spoken": "",
            "session_id": self.session_id,
            "expires_at": time.time() + max(15, cfg.FOLLOW_QUESTION),
            "target": actions.capture_target() if hasattr(actions, "capture_target") else None,
        }
        self._ui(status="asking", action=description, confirmation=dict(self.pending))
        self._say("Точно %s? Скажите «да» или «нет»." % description, status="asking")
        self.extend("confirmation")

    def _resolve_confirmation(self, text):
        pending = self.pending
        if pending is None:
            return
        if time.time() > pending["expires_at"] or pending["session_id"] != self.session_id:
            self.pending = None
            self._remaining_steps = []
            self._ui(confirmation=None)
            self._say("Подтверждение устарело, отменил.")
            self.extend("normal")
            return
        answer = re.sub(r"[^\w\s]", " ", text.lower().replace("ё", "е"))
        answer = re.sub(r"\s+", " ", answer).strip()
        yes = answer in ("да", "давай", "конечно", "подтверждаю", "ага", "точно", "yes", "да подтверждаю", "да сделай")
        no = answer in ("нет", "не надо", "отмена", "отмени", "no", "стоп", "отбой", "нет отмена")
        if yes:
            self.pending = None
            self._ui(confirmation=None)
            context = actions.restore_target(pending.get("target")) if hasattr(actions, "restore_target") else nullcontext()
            with context:
                if self._perform(pending["skill"], pending["params"], pending["remember_phrase"]):
                    self._continue_plan()
                else:
                    self._remaining_steps = []
        elif no:
            self.pending = None
            self._remaining_steps = []
            self._ui(confirmation=None, progress="Отменено")
            self._say("Отменил.")
        else:
            self._say("Подтвердите действие словами «да» или «нет».", status="asking")
        self.extend("confirmation" if self.pending else "action")

    def _perform(self, skill, params, remember_phrase=None, spoken=""):
        if self._cancelled():
            return False
        try:
            clean = skills.validate_params(skill, params)
            result = skills.execute(skill, clean)
            reply, success = result.message, result.ok
        except Exception as exc:
            clean = params or {}
            reply = "Команда не выполнилась: %s." % str(exc)
            success = False
        cancelled = self._cancelled()
        if remember_phrase and success and not cancelled:
            memory.remember(remember_phrase, skill, clean)
        try:
            memory.log_action(skill, clean, "ok" if success else "error", reply,
                              skills.risk(skill), self.session_id)
        except Exception:
            pass
        if cancelled:
            return False
        self._ui(error="" if success else reply,
                 progress="Выполнено %d из %d" % (self._plan_index, self._plan_total) if success else "Не выполнено")
        self._say(reply or ("Готово." if success else "Не удалось выполнить действие."))
        return success

    def _try_fast_path(self, quick_text):
        # Preliminary STT is display-only. Only final transcripts can execute.
        return False

    def _verify_wake(self, event):
        self.stats["wake_candidates"] += 1
        draft = self.ears.transcribe(event.audio, fast=True)
        called, command = ears_module.split_wake(draft)
        if not called:
            if cfg.DIAGNOSTIC_TEXT:
                self.log("  [wake rejected: %s]" % draft[:80])
            return False, draft, command
        return True, draft, command

    def run(self, stop_flag, paused_flag=None):
        def interrupted():
            return stop_flag() or bool(paused_flag and paused_flag())

        while not stop_flag():
            paused = bool(paused_flag and paused_flag())
            if paused:
                self.go_to_sleep("paused") if self.session_id else None
                self._ui(status="paused")
                time.sleep(0.25)
                continue

            if not self.active:
                self._ui(status="idle", level=0.0)
                self.ears.release_idle()
                event = self.ears.wait_for_wake(
                    stop_flag=lambda: stop_flag() or bool(paused_flag and paused_flag()),
                    on_level=None,
                )
                if interrupted():
                    return
                if event is None:
                    continue
                verified, draft, draft_command = self._verify_wake(event)
                if interrupted():
                    return
                if not verified:
                    self._ui(status="idle", heard="", reply="", level=0.0)
                    continue
                self.wake_up(event.engine)
                full = self.ears.transcribe_active(event.audio)
                if interrupted():
                    return
                self.stats["cloud_stt"] += int(self.ears.last_stt == "groq")
                called_full, command = ears_module.split_wake(full)
                if not called_full:
                    command = full
                if command.strip():
                    self.extend("normal")
                    self.active_until = max(self.active_until, time.time() + 1)
                    self.handle(command.strip())
                else:
                    self._say("Да, %s?" % cfg.TITLE)
                    self.extend("question")
                continue

            self._ui(status="listening")
            remaining = max(0.2, self.active_until - time.time())
            audio = self.ears.record(
                max_wait=min(1.5, remaining),
                stop_flag=lambda: stop_flag() or bool(paused_flag and paused_flag()),
                on_level=lambda value: self._ui(level=value),
                on_partial=(lambda text: self._ui(heard=text, status="hearing")),
                on_speech_start=lambda: self._ui(status="hearing"),
            )
            self._ui(level=0.0)
            if interrupted():
                return
            if audio is None:
                if self.should_sleep():
                    self.go_to_sleep("silence")
                continue
            self.last_activity = time.time()
            text = self.ears.transcribe_active(audio)
            if interrupted():
                return
            self.stats["cloud_stt"] += int(self.ears.last_stt == "groq")
            if text:
                # Speech began inside an authorized session. Recognition may
                # outlast the short follow-up timer; do not drop that command.
                self.extend("normal")
                self.active_until = max(self.active_until, time.time() + 1)
                self.handle(text)

    def summary(self):
        return ("правила %d · память %d · AI %d · STT cloud %d · не понял %d"
                % (self.stats["rules"], self.stats["learned"], self.stats["ai"],
                   self.stats["cloud_stt"], self.stats["unknown"]))

    def snapshot(self):
        return {
            "state": self.state,
            "active": self.active,
            "session_id": self.session_id,
            "active_until": self.active_until,
            "pending": dict(self.pending) if self.pending else None,
            "dictation": self.dictation,
            "stats": dict(self.stats),
            "summary": self.summary(),
        }

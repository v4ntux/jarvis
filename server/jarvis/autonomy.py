"""Finishing what was asked: no questions, a plan for big tasks, retries, another way when one fails.

RULES go into the prompts. The rest is the harness: it notices when the model stops with a question, an
excuse or an empty «done» and pushes it on, and it keeps the plan of a long task honest.
"""
import re
from dataclasses import dataclass

RULES = """Задачи доводи до результата сам:
- Владелец дал полную свободу: решай сам, мелочи не спрашивай (какой вариант, кому именно, подтверди, уточни). Выбери
  самый разумный вариант и делай. Спрашивать можно только о необратимо опасном: удаление важных данных, форматирование,
  деньги и платежи, массовые рассылки. Всё остальное делай сам.
- Задача из трёх и больше действий: сначала вызови plan — короткий список шагов, у каждого что считается успехом
  (done_when) и запасной способ (fallback). Иди по порядку, после каждого шага проверь результат по факту (вывод команды,
  look, ответ инструмента) и отметь plan_step.
- Шаг не вышел — повтори иначе (другой инструмент, путь или параметры), всего три попытки. И после этого не
  останавливайся и не извиняйся: придумай другой способ получить тот же результат (команда вместо кликов, браузер
  сервера вместо ноутбука, одно сообщение со ссылками вместо двадцати отправок) и обнови plan.
- «Слишком долго», «слишком много кликов», «не получится» — не причина остановиться: ищи короткий путь к тому же
  результату. Не предлагай варианты на выбор, выбирай сам.
- Не заканчивай вопросом. Закончил — дай результат: что сделано (факты, ссылки, цифры), чего не удалось и почему, что
  пробовал."""

TASK = re.compile(
    r"\b(сдела[йт]|отправ|скин[ьу]|найд|найти|открой|закро|запуст|скача|напиш|позвон|включ|выключ|постав|созда|удал|"
    r"покаж|провер|установ|добав|измен|перешл|залей|оформ|закаж|собер|состав|подготов|переведи|загрузи|сохрани|купи|"
    r"забронир|разбер|зайди|нажм|выбер|скопир|вставь|распакуй|архивир|запиши|сними|сфот|отредактир|исправ|почин|"
    r"настро|обнови|перезапуст|подпиш|отпиш|"
    r"sdelay|otprav|skin|nayd|otkro|zapust|skacha|napish|pozvon|vklyuch|vikluch|postav|sozday|pokaj|prover|ustanov|"
    r"dobav|izmen|peresh|zalej|oformi|zakaj|soberi|sostav|podgotov|zagruz|sohran|kupi|zaydi|najm|skopir|vstav)\w*"
    r"|\[Задача:", re.I)
SEQUENCE = re.compile(r"\b(потом|затем|после этого|а потом|далее|сначала|и ещё|и еще)\b|^\s*(\d+[.)]|[-•])\s", re.I | re.M)

DECISION = re.compile(r"какой|какую|каким|какое|который|кому|куда|откуда|как именно|когда именно|уточни|подтверд|"
                      r"хочешь|хотите|нужно ли|стоит ли|вариант|\bили\b|скажи|дай мне|пришли мне", re.I)
ASK_STATEMENT = re.compile(r"нужен (точн\w+|ник|username|юзернейм)|скажи (мне )?(точн|ник|как|какой|кому)|уточни|"
                           r"подтверди|(дай|пришли) (мне )?(точн|ник|ссылк)|как только скажешь|"
                           r"если хочешь,? (поищу|сделаю|отправлю|могу)|предлагаю", re.I)
GIVES_UP = re.compile(r"не могу\b|не смог\b|не получилось|не получится|не удалось|невозможно|не умею|нет возможности|"
                      r"к сожалению|слишком долго|слишком много (клик|шаг)", re.I)
CLAIMS_DONE = re.compile(r"\b(сделал|отправил|скинул|открыл|закрыл|запустил|скачал|написал|позвонил|включил|"
                         r"выключил|поставил|создал|удалил|нашёл|нашел|готово|выполнил|добавил|сохранил)\b", re.I)

NUDGE_ASK = ("[система] Владелец запретил вопросы и варианты на выбор. Не спрашивай и не предлагай: выбери сам самый "
             "разумный путь и доведи задачу до результата. Не хватает данных — найди их сам (поиск, браузер, ноутбук, "
             "история чата) или возьми разумное значение по умолчанию.")
NUDGE_GIVE_UP = ("[система] Останавливаться нельзя. Способ не сработал — придумай другой: другой инструмент, обход, "
                 "более короткий путь к тому же результату, и пробуй дальше. Если и это не выйдет, в конце честно "
                 "напиши, что пробовал и чего не удалось, без вопросов.")
NUDGE_CLAIM = ("[система] Ты написал, что сделал, но не вызвал ни одного инструмента. Сделай это по-настоящему "
               "сейчас и проверь результат.")
NUDGE_PLAN = "[система] По плану остались шаги: {steps}. Продолжай с ближайшего, не останавливайся."
NUDGE_COMPLEX = ("[система] Задача оказалась большой. Вызови plan: коротко распиши оставшиеся шаги с done_when и "
                 "fallback, и иди по ним.")
# Dithering (a question, an excuse, an empty «done») hands the task to the smarter model; a forgotten plan mark does not
SMART_NUDGES = (NUDGE_ASK, NUDGE_GIVE_UP, NUDGE_CLAIM)
MAX_NUDGES = 3
MAX_STEPS_IN_PLAN = 12
MAX_ATTEMPTS = 3


# «а это когда надо скинуть?», «зачем ты звонил?»: a question about a thing to do is talk, not a job
QUESTION = re.compile(r"^\W*(?:а\s+|a\s+)?(?:это\s+|eto\s+)?(?:когда|зачем|почему|что|чё|че|как|где|кто|сколько|какой|"
                      r"какая|какие|kogda|zachem|pochemu|shto|chto|cho|kak|gde|kto|skolko|kakoy|kakaya|kakiye)(?!\w)",
                      re.I)


def looks_like_task(text: str) -> bool:
    text = (text or "").strip()
    if QUESTION.search(text) and text.endswith("?") and len(text) < 120:
        return False
    return bool(TASK.search(text))


def is_complex(text: str) -> bool:
    """Worth a written plan before the first tool call."""
    text = (text or "").strip()
    verbs = len(TASK.findall(text))
    return verbs >= 3 or (verbs >= 2 and bool(SEQUENCE.search(text))) or (verbs >= 1 and len(text) > 220)


def asks_user(answer: str) -> bool:
    tail = answer.strip()[-320:]
    return ("?" in tail and bool(DECISION.search(tail))) or bool(ASK_STATEMENT.search(tail))


def gives_up(answer: str) -> bool:
    return bool(GIVES_UP.search(answer))


def unfinished(answer: str, task: bool, tools_used: bool, plan: "Plan") -> str:
    """What to tell the model when it stops too early ('' when the answer is final)."""
    text = answer.strip()
    if not text:
        return ""
    working = task or plan.started
    if working and asks_user(text):
        return NUDGE_ASK
    if working and gives_up(text):
        return NUDGE_GIVE_UP
    if task and not tools_used and CLAIMS_DONE.search(text):
        return NUDGE_CLAIM
    if plan.open():
        return NUDGE_PLAN.format(steps="; ".join(f"{i}) {s.title}" for i, s in plan.open()))
    return ""


@dataclass
class Step:
    title: str
    done_when: str = ""
    fallback: str = ""
    status: str = "todo"  # todo | done | failed
    fails: int = 0


class Plan:
    """The steps of one long task, with attempts counted per step."""

    def __init__(self) -> None:
        self.steps: list[Step] = []
        self.escalate = False  # a step failed twice: the smarter model takes over

    @property
    def started(self) -> bool:
        return bool(self.steps)

    def open(self) -> list[tuple[int, Step]]:
        return [(i, s) for i, s in enumerate(self.steps, 1) if s.status != "done"]

    def set(self, raw: list) -> str:
        steps = []
        for item in (raw or [])[:MAX_STEPS_IN_PLAN]:
            if isinstance(item, str):
                item = {"title": item}
            if isinstance(item, dict) and str(item.get("title", "")).strip():
                steps.append(Step(str(item["title"]).strip(), str(item.get("done_when", "")).strip(),
                                  str(item.get("fallback", "")).strip()))
        if not steps:
            return "Ошибка: в плане нет ни одного шага с title."
        self.steps = steps
        listing = "\n".join(f"{i}) {s.title}" + (f" [успех: {s.done_when}]" if s.done_when else "")
                            for i, s in enumerate(steps, 1))
        return f"План записан:\n{listing}\nИди по порядку, после каждого шага проверь результат и вызови plan_step."

    def mark(self, n: int, status: str, note: str = "") -> str:
        if not 1 <= n <= len(self.steps):
            return f"Ошибка: в плане нет шага {n}."
        step = self.steps[n - 1]
        if status == "done":
            step.status = "done"
            left = self.open()
            if not left:
                return "Все шаги выполнены. Дай итог: что сделано, с фактами и ссылками. Без вопросов."
            i, nxt = left[0]
            return f"Шаг {n} готов. Дальше шаг {i}: {nxt.title}" + (f" [успех: {nxt.done_when}]" if nxt.done_when else "")
        step.status, step.fails = "failed", step.fails + 1
        if step.fails >= 2:
            self.escalate = True
        if step.fails < MAX_ATTEMPTS:
            hint = f" Запасной способ: {step.fallback}." if step.fallback else ""
            return (f"Шаг {n} не вышел (попытка {step.fails} из {MAX_ATTEMPTS}){': ' + note if note else ''}. "
                    f"Попробуй иначе: другой инструмент, путь или параметры.{hint} Не сдавайся.")
        return (f"Шаг {n} не вышел {MAX_ATTEMPTS} раза. Не останавливайся и не спрашивай: придумай ДРУГОЙ способ "
                "получить тот же результат (обход, другой инструмент, более короткий путь) и вызови plan с "
                "обновлённым планом.")

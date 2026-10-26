"""The owner's /commands: instant, no model, no money. A command answers with text (empty: it has said its piece)."""
from __future__ import annotations

import time

from . import clock, watch
from .store import Store

HELP = ("Просто пиши или говори, как человеку. Команды без ИИ: /stop стоп всему · /reset забыть разговор (факты и уроки "
        "остаются) · /limit сбросить или поднять лимит на сегодня · /voice голосовые ответы вкл/выкл · /cost траты · /status что с чем · /monitor сайты и серверы · /screen скриншот ноутбука · "
        "/memory факты · /lessons уроки (/unlesson 3 — забыть урок) · /watchers наблюдатели (/unwatch 3) · /groups и "
        "/group 2 тихо|говори|выкл · /calltest позвонить в Telegram · /phonetest [call] SMS или звонок на телефон")
GROUP_MODES = {"on": "говорит", "quiet": "тихо, только когда зовут", "off": "выключен"}
MODE_WORDS = {"on": "on", "вкл": "on", "говори": "on", "включи": "on", "quiet": "quiet", "тихо": "quiet",
              "off": "off", "выкл": "off", "выключи": "off"}


class Commands:
    def __init__(self, app):
        self.app = app

    async def run(self, text: str) -> str | None:
        words = text[1:].split(maxsplit=1)
        if not words:
            return None
        fn = getattr(self, "cmd_" + words[0].split("@")[0].lower(), None)
        return await fn(words[1].strip() if len(words) > 1 else "") if fn else None

    @property
    def store(self) -> Store:
        return self.app.store

    async def cmd_start(self, arg: str) -> str:
        return HELP

    cmd_help = cmd_start

    async def cmd_stop(self, arg: str) -> str:
        return "" if await self.app.core.stop_all("owner") else "сейчас ничего не делаю"

    async def cmd_reset(self, arg: str) -> str:
        self.store.fresh_start("owner")
        return "забыл разговор. факты, уроки и навыки на месте"

    async def cmd_limit(self, arg: str) -> str:
        """/limit shows the day's money; /limit reset counts from zero again; /limit 5 sets today's limit to $5."""
        budget, word = self.app.core.budget, arg.strip().lower().replace(",", ".").lstrip("$")
        if word in ("reset", "сброс", "сбрось", "0"):
            had = budget.reset_count()
            return f"сбросил: считаю с нуля (было ${had:.2f}). лимит на сегодня ${budget.daily:.2f}"
        if word:
            try:
                amount = float(word)
            except ValueError:
                return "напиши так: /limit reset, или /limit 5 (лимит на сегодня в долларах)"
            if not 0.2 <= amount <= 100:
                return "лимит от $0.2 до $100"
            budget.set_today(amount)
            return f"лимит на сегодня ${amount:.2f} (завтра вернётся ${self.app.cfg.daily_limit:.2f}). потрачено ${budget.spent_today():.2f}"
        return (f"потрачено ${budget.spent_today():.2f} из ${budget.daily:.2f} на сегодня. /limit reset обнуляет счёт, "
                f"/limit 5 ставит лимит на сегодня. Постоянно: J4_DAILY_LIMIT в службе (сейчас ${self.app.cfg.daily_limit:.2f})")

    async def cmd_voice(self, arg: str) -> str:
        core = self.app.core
        core.voice_replies = not core.voice_replies
        return "на голосовые отвечаю и голосом" if core.voice_replies else "отвечаю только текстом"

    async def cmd_cost(self, arg: str) -> str:
        app, today = self.app, clock.day_start()
        used, rub = self.store.phone_since(today)
        return (app.core.budget.summary() + f"\nВсего за всё время: ${self.store.spent_since(0):.3f}\n"
                f"Лимит ${app.core.budget.daily:.2f} в день; после ${app.core.budget.soft:.2f} отвечает только дешёвая модель, "
                f"резерв Вану ${app.cfg.owner_reserve:.2f}.\nSMS и звонки на телефон сегодня: {used} из "
                f"{app.cfg.phone_daily_limit}, {rub:.2f} ₽")

    async def cmd_status(self, arg: str) -> str:
        app = self.app
        acc = app.account
        tg = ("на связи, звонки " + ("работают" if acc.callapi else "не работают (нет GEMINI_API_KEY или py-tgcalls)")
              if acc.ready else "НЕ подключён")
        extra = f"\nЛимиты Telegram: {acc.client.pace.status()}" if acc.ready and getattr(acc.client, "pace", None) else ""
        lines = [f"Ноутбук: {'на связи' if app.hub.online else 'не подключён'}",
                 f"Свой Telegram: {tg}{extra}",
                 f"Gemini (глаза для видео и гифок, голос звонков): {'есть' if app.gem.ready else 'нет ключа'}",
                 f"SMS и звонки на телефон: {'настроены' if app.phone.ready else 'не настроены'}",
                 f"Телефон (Android): {'на связи' if app.android and app.android.online else 'не подключён' if app.android else 'не настроен'}",
                 "Другие серверы: " + (", ".join(app.ssh.names()) or "нет"),
                 f"Сегодня потрачено ${app.core.budget.spent_today():.3f} из ${app.core.budget.daily:.2f}"]
        if app.cfg.dry_run:
            lines.append("⚠️ РЕПЕТИЦИЯ: ничего реально не отправляю, только пишу в журнал")
        if app.core.frozen():
            lines.append(f"После «стоп»: наблюдатели и таймеры молчат ещё {int(app.core.frozen_until - time.time())} с")
        return "\n".join(lines)

    async def cmd_monitor(self, arg: str) -> str:
        return self.app.monitor.status()

    async def cmd_screen(self, arg: str) -> str:
        lap = self.app.laptop
        if not lap.online:
            return "ноутбук не подключён"
        try:
            data = await lap.screenshot()
        except Exception as e:
            return f"не вышло: {e}"
        await self.app.core.out_for("owner").send_file("screen.png", data)
        return ""

    async def cmd_memory(self, arg: str) -> str:
        return "\n".join(f"#{i}: {t}" for i, t in self.store.facts()) or "пока ничего не запомнил"

    async def cmd_lessons(self, arg: str) -> str:
        rows = self.store.lessons()
        return ("\n".join(f"#{i}: {t}" for i, t, _ in rows) + "\n\nзабыть: /unlesson 3") if rows else \
            "уроков пока нет: они появляются, когда ты меня поправляешь"

    async def cmd_unlesson(self, arg: str) -> str:
        number = arg.lstrip("#")
        if not number.isdigit():
            return "напиши номер: /unlesson 3"
        return f"забыл урок #{number}" if self.store.drop_lesson(int(number)) else f"урока #{number} нет"

    async def cmd_watchers(self, arg: str) -> str:
        rows = self.store.watchers()
        if not rows:
            return "наблюдателей нет. скажи, например: «скажи, когда закончится загрузка» или «каждое утро в 8 присылай погоду»"
        return "\n".join(f"#{i} {watch.describe(s)}" + (f"\n   ⚠️ {st['error']}" if st.get("error") else "")
                         for i, s, st, _ in rows) + f"\n\nудалить: /unwatch {rows[0][0]}"

    async def cmd_unwatch(self, arg: str) -> str:
        number = arg.lstrip("#")
        if not number.isdigit():
            return "напиши номер: /unwatch 3 (номера покажет /watchers)"
        return f"удалил #{number}" if self.store.remove_watcher(int(number)) else f"наблюдателя #{number} нет"

    async def cmd_groups(self, arg: str) -> str:
        rows = self.store.chats("group")
        if not rows:
            return "я пока ни в одной группе. добавь меня в чат, и я там поживу"
        today = clock.day_start()
        lines = [f"{n}. «{title}»: {GROUP_MODES.get(mode, mode)}, сегодня сказал {self.store.count_msgs(key, 'jarvis', today)}"
                 for n, (key, _, title, mode) in enumerate(rows, 1)]
        return "\n".join(lines) + "\n\nуправление: /group 2 тихо (или говори, выкл): номер или кусок названия"

    async def cmd_group(self, arg: str) -> str:
        words = arg.split()
        mode = MODE_WORDS.get(words[-1].lower()) if len(words) >= 2 else None
        if mode is None:
            return "напиши так: /group 2 тихо (или говори, выкл). номера покажет /groups"
        which = " ".join(words[:-1]).lower()
        found = [row for n, row in enumerate(self.store.chats("group"), 1) if which == str(n) or which in row[2].lower()]
        if len(found) != 1:
            return f"не пойму, какая группа «{which}»: нашлось {len(found)}. смотри /groups"
        self.store.set_chat_mode(found[0][0], mode)
        return f"«{found[0][2]}»: {GROUP_MODES[mode]}"

    async def cmd_calltest(self, arg: str) -> str:
        app = self.app
        if not app.account.callapi:
            return "звонки не работают: /status покажет почему"
        app.core.spawn(app.alerts.ring("Проверка звонка. Скажи что-нибудь, я отвечу. Чтобы закончить, скажи «пока»."))
        return "звоню в Telegram"

    async def cmd_phonetest(self, arg: str) -> str:
        phone = self.app.phone
        kind = "call" if "call" in arg.lower() else "sms"
        try:
            return await (phone.call if kind == "call" else phone.sms)("Джарвис: проверка связи. Если слышишь или читаешь это, всё работает.")
        except Exception as e:
            return f"не вышло: {e}"

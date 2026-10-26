"""The guard: every action passes it before it runs. It lives in code, so the model cannot forget a rule.

Three answers: allow, ask the owner once (a red-list action), or deny (never, whatever anyone says).
The red list is what the owner agreed to: money, deleting important data, writing first to strangers or mass
sends to new people, and changing his own code without tests. Everything else Jarvis simply does.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Verdict:
    kind: str  # allow | ask | deny
    text: str = ""  # the question for the owner, or why it is refused
    scope: str = ""  # what a yes covers: asked once per scope in a task

    @property
    def allowed(self) -> bool:
        return self.kind == "allow"


ALLOW = Verdict("allow")


def ask(question: str, scope: str) -> Verdict:
    return Verdict("ask", question, scope)


def deny(reason: str) -> Verdict:
    return Verdict("deny", reason)


# Secrets never go through a shell: the Telegram session (a second client on it can cost the account), the keys.
# Packing .env into a backup archive is fine, reading or sourcing it is not.
SESSION = re.compile(r"tg_user\.session|\w+\.session\b|TelegramClient|\btelethon\b", re.I)
ENV_READ = re.compile(
    r"(?:\b(?:cat|less|more|head|tail|grep|sed|awk|strings|xxd|base64|type|get-content|gc)\b|(?:^|[;&|(]\s*)(?:\.|source)\s)"
    r"[^|;&\n]*\.env\b"
    r"|\bprintenv\b|\$\{?(?:ANTHROPIC_API_KEY|TG_API_HASH|TG_API_ID|GEMINI_API_KEY|HANDS_TOKEN|SMSC_PASSWORD)\b",
    re.I)
# Cutting himself off from the machine
LOCKOUT = re.compile(
    r"\bufw\s+(disable|reset|deny|delete)|iptables\s+-[FPX]|netsh\s+advfirewall|"
    r"systemctl\s+(stop|disable|mask|kill)\s[^|;&]*\b(sshd?|tailscaled)\b|\bpasswd\b|\bchpasswd\b|\buserdel\b|"
    r"authorized_keys|sshd_config", re.I)

# The red list for shells
RED_DELETE = re.compile(
    r"\brm\b[^|;&]*?\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(\s|$)|remove-item\b[^|;]*-recurse|"
    r"\b(rmdir|rd|del|erase)\b[^|;&]*\s/s\b|\b(shred|truncate)\b|format-volume|clear-disk|diskpart|"
    r"\bformat\s+[a-z]:|\bmkfs|\bdd\s[^|;]*of=/dev/|bcdedit|cipher\s+/w|reg\s+delete|remove-itemproperty|"
    r"git\s+(push\s[^|;]*(--force|-f\b)|reset\s+--hard|clean\s+-\w*f)", re.I)
RED_POWER = re.compile(r"stop-computer|restart-computer|\bshutdown\b|\breboot\b|\bpoweroff\b|\bhalt\b", re.I)
RED_PIPE_TO_SHELL = re.compile(r"invoke-webrequest[^|]*\|\s*iex|\biex\s*\(|\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba)?sh\b",
                               re.I)
RED_SELF = re.compile(r"(/opt/jarvis/jarvis4?\b|(^|[\s/;&])jarvis4?/\w+\.py\b|/etc/systemd/system/jarvis)", re.I)
RED_SELF_WRITE = re.compile(r"sed\s+-i|\bcp\b|\bmv\b|\btee\b|>|\bpatch\b|\bgit\s+(checkout|apply|pull|reset|stash)|"
                            r"\bchmod\b|\bchown\b|\brm\b|write_text|open\(", re.I)
RED_SELF_SERVICE = re.compile(r"systemctl\s+(restart|stop|kill|disable|mask|daemon-reload)\b[^|;&]*\bjarvis|"
                              r"\bp?kill\b[^|;&]*(jarvis|python)", re.I)
# Paying: in a shell or on a screen
MONEY = re.compile(r"оплат|купить|заплат|перевест|перевод\s+на|pay\b|checkout|purchase|\bbuy\b|подтвердить\s+заказ|"
                   r"place\s+order|оформить\s+заказ", re.I)

WORK_DIRS = ("/opt/jarvis/data/work", "/opt/jarvis/data/inbox", "/tmp")


def shell(command: str, where: str) -> Verdict:
    """where: «laptop» or «server»."""
    if SESSION.search(command):
        return deny("сессию Telegram и клиентов к ней я не трогаю: так можно потерять аккаунт. Для Telegram есть "
                    "инструменты tg_*")
    if ENV_READ.search(command):
        return deny("ключи и .env не читаю и не выношу в команды. В архив их положить можно")
    if LOCKOUT.search(command):
        return deny("так я отрежу себе доступ к машине. Это пусть сделает Ван сам руками")
    if RED_SELF.search(command) and RED_SELF_WRITE.search(command):
        return ask("Хочу поменять собственный код. Ломать себя без проверки не буду. Менять?\n" + command[:300],
                   "self-edit")
    if RED_SELF_SERVICE.search(command):
        return ask("Перезапущу или остановлю свой сервис, на минуту пропаду. Ок?\n" + command[:300], "self-restart")
    if RED_PIPE_TO_SHELL.search(command):
        return ask("Эта команда запускает скрипт прямо из интернета. Запускать?\n" + command[:300], "pipe-shell")
    if RED_POWER.search(command):
        return ask(f"Это выключит или перезагрузит {'ноутбук' if where == 'laptop' else 'сервер'}. Точно?\n" + command[:300],
                   "power")
    if RED_DELETE.search(command) and not safe_delete(command):
        return ask("Это удаляет данные без возврата. Выполнить?\n" + command[:300], "delete")
    return ALLOW


def safe_delete(command: str) -> bool:
    """A plain `rm -r` inside Jarvis's own work, inbox or tmp folders is routine cleanup."""
    import os
    import shlex
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if not words or words[0] != "rm" or any(c in command for c in ";&|`$(<>"):
        return False
    targets = [w for w in words[1:] if not w.startswith("-")]
    roots = [os.path.realpath(d) for d in WORK_DIRS]
    for target in targets:
        path = os.path.realpath(os.path.join(WORK_DIRS[0], os.path.expanduser(target)))
        if path in roots or not any(path.startswith(root + os.sep) for root in roots):
            return False
    return bool(targets)


def screen_click(label: str) -> Verdict:
    """A click on a button that pays or confirms an order is a red-list action."""
    if MONEY.search(label or ""):
        return ask(f"Это похоже на оплату или заказ: «{label[:80]}». Нажимать?", f"money:{label[:40].lower()}")
    return ALLOW

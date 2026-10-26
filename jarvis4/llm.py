"""One door to Claude: the request with caching and effort, the money it cost, and errors in plain words.

Nothing here decides anything. The agent hands in a model, a system prompt, messages and tools; it gets back text,
tool calls, and the dollars spent (already written to the store).
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field

from . import budget
from .config import Config
from .store import Store

log = logging.getLogger("jarvis4.llm")

try:
    import anthropic
except ImportError:  # the laptop's test environment has no SDK; the real client is only made on the server
    anthropic = None


@dataclass
class Call:
    id: str
    name: str
    input: dict


@dataclass
class Reply:
    raw: list  # the content to put back into the messages as it came (thinking blocks included)
    text: str = ""
    calls: list[Call] = field(default_factory=list)
    stop: str = ""
    model: str = ""
    cost: float = 0.0
    refusal: bool = False


class LLMError(Exception):
    """kind: billing | auth | busy | bad | other"""

    def __init__(self, kind: str, text: str):
        super().__init__(text)
        self.kind, self.text = kind, text


def _cached(messages: list[dict]) -> list[dict]:
    """A copy whose newest message carries the rolling cache breakpoint; the originals stay as they are."""
    if not messages:
        return messages
    out = list(messages)
    last = copy.copy(out[-1])
    content = last["content"]
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    else:
        content = [dict(b) if isinstance(b, dict) else b for b in content]
    if content and isinstance(content[-1], dict) and content[-1].get("type") != "thinking":
        content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
    last["content"] = content
    out[-1] = last
    return out


def _block(b) -> dict:
    return b if isinstance(b, dict) else b.model_dump(exclude_none=True)


class LLM:
    def __init__(self, cfg: Config, store: Store, client=None):
        self.cfg, self.store = cfg, store
        if client is None and anthropic is not None and cfg.anthropic_key:
            client = anthropic.AsyncAnthropic(api_key=cfg.anthropic_key, max_retries=2)
        self.client = client

    @property
    def ready(self) -> bool:
        return self.client is not None

    async def complete(self, model: str, system: list[dict] | str, messages: list[dict], tools: list[dict] | None = None,
                       max_tokens: int = 4000, effort: str = "medium", kind: str = "", schema: dict | None = None) -> Reply:
        if self.client is None:
            raise LLMError("auth", "нет ключа Anthropic")
        args = dict(model=model, max_tokens=max_tokens, system=system, messages=_cached(messages))
        if tools:
            args["tools"] = tools[:-1] + [{**tools[-1], "cache_control": {"type": "ephemeral"}}]
        output = {"effort": effort}
        if schema:
            output["format"] = {"type": "json_schema", "schema": schema}
        args["output_config"] = output
        try:
            resp = await self.client.messages.create(**args)
        except Exception as e:
            raise self._error(e) from e
        u = resp.usage
        cost = budget.cost_of(self.cfg, model, u.input_tokens or 0, getattr(u, "cache_creation_input_tokens", 0) or 0,
                              getattr(u, "cache_read_input_tokens", 0) or 0, u.output_tokens or 0)
        self.store.log_usage(model, u.input_tokens or 0, u.output_tokens or 0, cost, kind)
        raw = [_block(b) for b in resp.content]
        text = "".join(b.get("text", "") for b in raw if b.get("type") == "text").strip()
        calls = [Call(b["id"], b["name"], b.get("input") or {}) for b in raw if b.get("type") == "tool_use"]
        refusal = resp.stop_reason == "refusal"
        if refusal:
            log.info("Claude declined: %s", getattr(getattr(resp, "stop_details", None), "category", None))
        return Reply(raw, "" if refusal else text, calls, resp.stop_reason or "", model, cost, refusal)

    @staticmethod
    def _error(e: Exception) -> LLMError:
        if anthropic is None or not isinstance(e, anthropic.APIError):
            return LLMError("other", f"{type(e).__name__}: {e}"[:300])
        text = str(e)
        if isinstance(e, anthropic.BadRequestError):
            return LLMError("billing" if "credit balance" in text else "bad", text[:400])
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            return LLMError("auth", text[:300])
        if isinstance(e, (anthropic.RateLimitError, anthropic.InternalServerError, anthropic.APIConnectionError)):
            return LLMError("busy", text[:300])
        return LLMError("other", text[:300])

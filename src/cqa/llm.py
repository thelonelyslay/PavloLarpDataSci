"""Minimal LLM clients over `requests` (Python 3.9, no SDKs): Anthropic, Gemini, OpenAI.

Provider is enabled only when its key is present in .env (or the environment).
Keys are never printed. Each call returns text, token usage, latency and cost.
"""
from __future__ import annotations

import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[2]

# Claude: ids and USD per 1M tokens (input, output), from Anthropic pricing (2026-10).
CLAUDE_MODELS = [
    ("claude-sonnet-5-5", 2.00, 10.00),
    ("claude-haiku-5-5", 0.10, 0.50),
    ("claude-opus-5-5", 4.00, 20.00),
]
ANTHROPIC_VERSION = "2026-10-21"

# Gemini 3/4 use thinkingLevel (thinkingBudget is for 2.5; sending both is a 400).
# Not every model supports every level (gemini-3.8-flash: no "minimal"), so "auto"
# walks up this list on a 400 and remembers the first level the model accepts.
GEMINI_LEVELS = ["minimal", "low", "medium", "high"]
_gemini_level_cache: dict[str, str] = {}
_cache_lock = threading.Lock()


class LLMError(RuntimeError):
    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


@dataclass
class ModelSpec:
    name: str            # display name, also used for the results file
    provider: str        # anthropic | gemini | openai
    model_id: str
    price_in: Optional[float] = None   # USD per 1M input tokens
    price_out: Optional[float] = None  # USD per 1M output tokens


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    seconds: float
    cost_usd: Optional[float]
    params: dict = field(default_factory=dict)


def load_env() -> dict:
    env = {k: v for k, v in dotenv_values(ROOT / ".env").items() if v}
    for k, v in os.environ.items():
        env.setdefault(k, v)
    return env


def _price(env: dict, key: str) -> Optional[float]:
    try:
        return float(env[key]) if env.get(key) else None
    except ValueError:
        return None


def available_models(env: dict) -> list[ModelSpec]:
    specs: list[ModelSpec] = []
    if env.get("ANTHROPIC_API_KEY"):
        specs += [ModelSpec(m, "anthropic", m, pi, po) for m, pi, po in CLAUDE_MODELS]
    if env.get("GEMINI_API_KEY") and env.get("GEMINI_MODEL"):
        m = env["GEMINI_MODEL"]
        specs.append(ModelSpec(m, "gemini", m, _price(env, "GEMINI_PRICE_IN"), _price(env, "GEMINI_PRICE_OUT")))
    if env.get("OPENAI_API_KEY") and env.get("OPENAI_MODEL"):
        m = env["OPENAI_MODEL"]
        specs.append(ModelSpec(m, "openai", m, _price(env, "OPENAI_PRICE_IN"), _price(env, "OPENAI_PRICE_OUT")))
    return specs


def missing_setup(env: dict) -> list[str]:
    """Human-readable hints about providers that are not configured."""
    hints = []
    if not env.get("ANTHROPIC_API_KEY"):
        hints.append("ANTHROPIC_API_KEY is empty (Claude skipped)")
    if not env.get("GEMINI_API_KEY") or not env.get("GEMINI_MODEL"):
        hints.append("GEMINI_API_KEY or GEMINI_MODEL is empty (Gemini skipped)")
    if not env.get("OPENAI_API_KEY") or not env.get("OPENAI_MODEL"):
        hints.append("OPENAI_API_KEY or OPENAI_MODEL is empty (OpenAI skipped)")
    return hints


def _post(url: str, headers: dict, body: dict, stats: dict, timeout: int = 180, retries: int = 2) -> dict:
    """POST with retries on 429/5xx/network errors. Error text never includes headers (keys).

    stats["http_retries"] counts retries: their waiting time is part of the measured latency.
    """
    for attempt in range(retries + 1):
        if attempt:
            stats["http_retries"] = stats.get("http_retries", 0) + 1
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.RequestException as e:
            if attempt == retries:
                raise LLMError(f"network error: {type(e).__name__}") from None
            time.sleep(2 ** (attempt + 1))
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == retries:
                raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code)
            wait = resp.headers.get("retry-after")
            time.sleep(float(wait) if wait and wait.replace(".", "", 1).isdigit() else 2 ** (attempt + 2))
            continue
        if resp.status_code >= 400:
            raise LLMError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code)
        return resp.json()
    raise LLMError("unreachable")


def _cost(spec: ModelSpec, tin: int, tout: int) -> Optional[float]:
    if spec.price_in is None or spec.price_out is None:
        return None
    return tin * spec.price_in / 1e6 + tout * spec.price_out / 1e6


def _call_anthropic(spec: ModelSpec, env: dict, system: str, user: str, schema: dict, opts: dict, stats: dict):
    # Current Claude models reject non-default temperature, so it is not sent;
    # determinism is controlled by structured output + explicit effort.
    effort = opts.get("effort", "low")
    body = {
        "model": spec.model_id,
        "max_tokens": 16000,
        "system": system,
        "messages": [{"role": "user", "content": user}],
        "output_config": {"effort": effort, "format": {"type": "json_schema", "schema": schema}},
    }
    headers = {
        "x-api-key": env["ANTHROPIC_API_KEY"],
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    data = _post("https://api.anthropic.com/v1/messages", headers, body, stats)
    stop = data.get("stop_reason")
    if stop in ("refusal", "max_tokens"):
        raise LLMError(f"stop_reason={stop}")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    u = data.get("usage") or {}
    tin = int(u.get("input_tokens", 0)) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0)
    return text, tin, int(u.get("output_tokens", 0)), {"effort": effort, "temperature": "not supported"}


def _gemini_schema(s):
    """JSON Schema -> Gemini responseSchema (OpenAPI subset: no additionalProperties)."""
    if isinstance(s, dict):
        return {k: _gemini_schema(v) for k, v in s.items() if k != "additionalProperties"}
    if isinstance(s, list):
        return [_gemini_schema(x) for x in s]
    return s


def gemini_takes_temperature(model_id: str) -> bool:
    """Gemini 3.6+ / 4: temperature is deprecated and ignored (400 in future versions), so don't send it."""
    m = re.match(r"gemini-(\d+)(?:\.(\d+))?", model_id)
    if not m:
        return True
    return (int(m.group(1)), int(m.group(2) or 0)) < (3, 6)


def _call_gemini(spec: ModelSpec, env: dict, system: str, user: str, schema: dict, opts: dict, stats: dict):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{spec.model_id}:generateContent"
    headers = {"x-goog-api-key": env["GEMINI_API_KEY"], "content-type": "application/json"}
    gen = {"responseMimeType": "application/json", "responseSchema": _gemini_schema(schema)}
    params: dict = {}
    if gemini_takes_temperature(spec.model_id):
        gen["temperature"] = 0
        params["temperature"] = 0
    else:
        params["temperature"] = "ignored by model (not sent)"

    want = opts.get("gemini_thinking", "auto")
    if want == "auto":
        with _cache_lock:
            level: Optional[str] = _gemini_level_cache.get(spec.model_id, GEMINI_LEVELS[0])
    else:
        level = None if want == "default" else want

    while True:
        if level:
            gen["thinkingConfig"] = {"thinkingLevel": level}
        else:
            gen.pop("thinkingConfig", None)
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": gen,
        }
        try:
            data = _post(url, headers, body, stats)
            break
        except LLMError as e:
            msg = str(e).lower()
            if e.status == 400 and "temperature" in msg and "temperature" in gen:
                gen.pop("temperature")
                params["temperature"] = "rejected by model (not sent)"
                continue
            # auto: the model rejected this thinking level -> try the next one up
            if (want == "auto" and e.status == 400 and level in GEMINI_LEVELS[:-1]
                    and ("thinking" in msg or level in msg)):
                level = GEMINI_LEVELS[GEMINI_LEVELS.index(level) + 1]
                stats["thinking_level_fallbacks"] = stats.get("thinking_level_fallbacks", 0) + 1
                continue
            raise
    if want == "auto" and level:
        with _cache_lock:
            _gemini_level_cache[spec.model_id] = level
    params["thinking_level"] = level or "model default"

    cands = data.get("candidates") or []
    if not cands:
        raise LLMError(f"no candidates (promptFeedback={data.get('promptFeedback')})")
    finish = cands[0].get("finishReason")
    if finish not in (None, "STOP"):
        raise LLMError(f"finishReason={finish}")
    text = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", [])
                   if not p.get("thought"))
    u = data.get("usageMetadata") or {}
    thoughts = int(u.get("thoughtsTokenCount", 0))
    params["thought_tokens"] = thoughts  # billed as output, already included in output_tokens
    return text, int(u.get("promptTokenCount", 0)), int(u.get("candidatesTokenCount", 0)) + thoughts, params


def _call_openai(spec: ModelSpec, env: dict, system: str, user: str, schema: dict, opts: dict, stats: dict):
    body = {
        "model": spec.model_id,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": 0,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "cqa_evaluation", "strict": True, "schema": schema}},
    }
    headers = {"authorization": f"Bearer {env['OPENAI_API_KEY']}", "content-type": "application/json"}
    url = "https://api.openai.com/v1/chat/completions"
    params = {"temperature": 0}
    try:
        data = _post(url, headers, body, stats)
    except LLMError as e:
        # reasoning models accept only the default temperature
        if "temperature" not in str(e):
            raise
        body.pop("temperature")
        params = {"temperature": "not supported"}
        data = _post(url, headers, body, stats)
    choice = (data.get("choices") or [{}])[0]
    if choice.get("finish_reason") not in (None, "stop"):
        raise LLMError(f"finish_reason={choice.get('finish_reason')}")
    msg = choice.get("message") or {}
    if msg.get("refusal"):
        raise LLMError("refusal")
    u = data.get("usage") or {}
    return msg.get("content") or "", int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), params


_CALLERS = {"anthropic": _call_anthropic, "gemini": _call_gemini, "openai": _call_openai}


def call(spec: ModelSpec, env: dict, system: str, user: str, schema: dict,
         opts: Optional[dict] = None) -> LLMResult:
    """opts: {"effort": Claude effort, "gemini_thinking": auto|minimal|low|medium|high|default}."""
    stats: dict = {}
    t0 = time.perf_counter()
    text, tin, tout, params = _CALLERS[spec.provider](spec, env, system, user, schema, opts or {}, stats)
    params.update(stats)
    return LLMResult(text, tin, tout, time.perf_counter() - t0, _cost(spec, tin, tout), params)

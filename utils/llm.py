"""
utils/llm.py
------------
Factory for Groq-backed ChatGroq instances.
Centralises model selection, global throttling, and rate-limit handling.

Groq free tier has separate TPM/TPD budgets per model. When the smart model
hits a daily (TPD) limit, waiting a few seconds never helps — we must fall
back to the fast model or a heuristic instead of spinning on retries.
"""

from __future__ import annotations
import re
import threading
import time
from functools import lru_cache
from typing import Any

from langchain_groq import ChatGroq
from config.settings import settings

# Serialize LLM calls so parallel research/scoring cannot stampede the API.
_llm_lock = threading.Lock()
_last_call_at = 0.0

# After a hard daily-limit hit on a model, skip it for a while.
_model_cooldown_until: dict[str, float] = {}
_cooldown_lock = threading.Lock()


class RateLimitExhausted(Exception):
    """Raised when the provider asks us to wait longer than is practical."""

    def __init__(self, message: str, wait_seconds: float = 0.0, model: str = ""):
        super().__init__(message)
        self.wait_seconds = wait_seconds
        self.model = model


def _model_name(llm) -> str:
    return getattr(llm, "model_name", None) or getattr(llm, "model", None) or ""


def _is_cooling_down(model: str) -> bool:
    if not model:
        return False
    with _cooldown_lock:
        until = _model_cooldown_until.get(model, 0.0)
        return time.time() < until


def _set_cooldown(model: str, seconds: float) -> None:
    if not model:
        return
    # Cap stored cooldown so we re-check later; still skip for a useful window
    seconds = max(30.0, min(seconds, 3600.0))
    with _cooldown_lock:
        _model_cooldown_until[model] = time.time() + seconds


def _parse_retry_after(exc: Exception) -> float | None:
    """Extract 'try again in Xs' from Groq error text."""
    m = re.search(r"try again in\s+([\d.]+)\s*s", str(exc), re.I)
    if m:
        return float(m.group(1))
    # e.g. "try again in 28m31.584s"
    m = re.search(r"try again in\s+(\d+)m([\d.]+)s", str(exc), re.I)
    if m:
        return int(m.group(1)) * 60 + float(m.group(2))
    m = re.search(r"try again in\s+(\d+)m", str(exc), re.I)
    if m:
        return int(m.group(1)) * 60.0
    return None


def _is_rate_limit(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "rate_limit",
            "rate limit",
            "429",
            "tokens per minute",
            "tokens per day",
            "tpm",
            "tpd",
        )
    )


def _is_retryable(exc: Exception) -> bool:
    if _is_rate_limit(exc):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in ("timeout", "temporarily", "overloaded", "503", "502", "connection")
    )


@lru_cache(maxsize=8)
def get_llm(model: str | None = None, temperature: float = 0.3) -> ChatGroq:
    """
    Return a cached ChatGroq instance.
    Use fast_model for classification/scoring, smart_model for generation.
    """
    model = model or settings.smart_model
    return ChatGroq(
        model=model,
        api_key=settings.groq_api_key,
        temperature=temperature,
        # We handle retries ourselves — nested retries multiply wait time.
        max_retries=0,
    )


def fast_llm(temperature: float = 0.1) -> ChatGroq:
    return get_llm(settings.fast_model, temperature)


def smart_llm(temperature: float = 0.4) -> ChatGroq:
    return get_llm(settings.smart_model, temperature)


def _min_gap() -> float:
    return max(0.5, settings.llm_min_gap_sec)


def _throttle() -> None:
    global _last_call_at
    with _llm_lock:
        now = time.time()
        gap = _min_gap() - (now - _last_call_at)
        if gap > 0:
            time.sleep(gap)
        _last_call_at = time.time()


def invoke_with_retry(
    llm,
    messages,
    *,
    max_attempts: int = 4,
    label: str = "LLM",
    max_wait_sec: float = 45.0,
):
    """
    Call llm.invoke with backoff on short-lived rate limits / transient errors.

    If Groq asks us to wait longer than max_wait_sec (typical of daily TPD
    exhaustion), raise RateLimitExhausted immediately so callers can fall back.
    """
    model = _model_name(llm)
    if _is_cooling_down(model):
        raise RateLimitExhausted(
            f"Model {model} is in cooldown after a daily rate limit",
            wait_seconds=0,
            model=model,
        )

    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            _throttle()
            return llm.invoke(messages)
        except Exception as e:
            last_err = e
            if not _is_retryable(e):
                raise

            wait = _parse_retry_after(e)
            if wait is None:
                wait = min(2 ** attempt, 20)
            else:
                wait = wait + 0.5

            # Daily / long waits: don't spin — escalate to fallback path
            if wait > max_wait_sec:
                _set_cooldown(model, wait)
                from utils.helpers import log_agent
                log_agent(
                    label,
                    f"Daily/long rate limit on {model or 'model'} "
                    f"(~{wait/60:.0f}m) — switching to fallback",
                    "warn",
                )
                raise RateLimitExhausted(str(e), wait_seconds=wait, model=model) from e

            if attempt >= max_attempts:
                if _is_rate_limit(e):
                    raise RateLimitExhausted(str(e), wait_seconds=wait, model=model) from e
                raise

            from utils.helpers import log_agent
            log_agent(
                label,
                f"Rate limited — retrying in {wait:.1f}s (attempt {attempt}/{max_attempts})",
                "warn",
            )
            time.sleep(wait)

    raise last_err  # pragma: no cover


def invoke_smart_or_fast(
    messages,
    *,
    temperature: float = 0.3,
    label: str = "LLM",
    prefer_fast: bool = False,
) -> Any:
    """
    Prefer smart model, fall back to fast model on rate-limit / failure.
    Set prefer_fast=True to skip the smart model entirely (saves TPD budget).
    """
    errors: list[str] = []

    order = []
    if prefer_fast or settings.groq_prefer_fast or _is_cooling_down(settings.smart_model):
        order = [("fast", fast_llm(temperature))]
    else:
        order = [
            ("smart", smart_llm(temperature)),
            ("fast", fast_llm(temperature)),
        ]

    # If smart is cooling down we already only have fast; if fast is also cooling,
    # still try once — cooldown is advisory.
    if not prefer_fast and _is_cooling_down(settings.smart_model):
        from utils.helpers import log_agent
        log_agent(label, "Smart model cooling down — using fast model", "warn")

    for name, llm in order:
        if name == "fast" and _is_cooling_down(settings.fast_model) and len(order) > 1:
            continue
        try:
            return invoke_with_retry(llm, messages, label=label, max_attempts=3)
        except RateLimitExhausted as e:
            errors.append(f"{name}: rate limited (~{e.wait_seconds:.0f}s)")
            continue
        except Exception as e:
            if _is_rate_limit(e):
                errors.append(f"{name}: {e}")
                continue
            errors.append(f"{name}: {e}")
            # Non-rate errors on smart → still try fast
            if name == "smart":
                continue
            raise

    raise RateLimitExhausted(
        "All LLM models unavailable: " + "; ".join(errors),
        wait_seconds=0,
        model=settings.smart_model,
    )

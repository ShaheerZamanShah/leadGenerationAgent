"""
utils/llm.py
------------
Factory for Groq-backed ChatGroq instances.
Centralises model selection and retry configuration.
"""

from __future__ import annotations
from functools import lru_cache
from langchain_groq import ChatGroq
from tenacity import retry, stop_after_attempt, wait_exponential
from config.settings import settings


@lru_cache(maxsize=4)
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
        max_retries=3,
    )


def fast_llm(temperature: float = 0.1) -> ChatGroq:
    return get_llm(settings.fast_model, temperature)


def smart_llm(temperature: float = 0.4) -> ChatGroq:
    return get_llm(settings.smart_model, temperature)

"""Async HTTP client for the local vLLM OpenAI-compatible endpoint.

Module-level singleton ``AsyncClient`` shared across tool invocations so the
underlying HTTP/2 connection pool is reused. Disables Qwen3.5 chat-template
thinking by default (the fine-tune was trained without ``<think>`` blocks).
"""

from __future__ import annotations

import os

import httpx


_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
_MODEL = os.environ.get("VLLM_MODEL", "qsql-qwen35-9b")
_MAX_TOKENS = int(os.environ.get("VLLM_MAX_TOKENS", "2048"))
_TIMEOUT_S = float(os.environ.get("VLLM_TIMEOUT_S", "120"))


_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=_TIMEOUT_S, write=10.0, pool=5.0),
            headers={"Content-Type": "application/json"},
        )
    return _client


async def aclose() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


async def chat(
    messages: list[dict],
    *,
    temperature: float = 0.2,
    top_p: float = 0.95,
    max_tokens: int | None = None,
    n: int = 1,
) -> list[str]:
    """POST to ``/chat/completions`` and return all ``n`` completion strings.

    Uses the OpenAI-compatible ``n`` field (vLLM implements it natively via
    ``SamplingParams(n=...)``) to draw ``n`` independent samples in a single
    request/prefill rather than issuing ``n`` separate HTTP calls — cheaper
    (prompt is only prefilled once) and lets vLLM schedule/batch the decode
    paths together atomically.

    ``n>1`` only yields diverse completions when ``temperature>0`` — at
    temperature 0 (greedy) vLLM returns ``n`` copies of the same output,
    silently wasting the sampling budget. Callers doing self-consistency
    sampling must pass a non-zero temperature.

    Returned list preserves ``choices[]`` order (``choices[i].index==i``);
    callers that care about "generation order" (e.g. tie-breaking within a
    winning cluster) can rely on list order.
    """
    body = {
        "model": _MODEL,
        "messages": messages,
        "temperature": temperature,
        "top_p": top_p,
        "max_tokens": max_tokens if max_tokens is not None else _MAX_TOKENS,
        "n": n,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    resp = await _get_client().post("/chat/completions", json=body)
    resp.raise_for_status()
    data = resp.json()
    choices = sorted(data["choices"], key=lambda c: c["index"])
    return [c["message"]["content"] for c in choices]

"""Async HTTP client for the local vLLM OpenAI-compatible endpoint.

Module-level singleton ``AsyncClient`` shared across tool invocations so the
underlying HTTP/2 connection pool is reused. Disables Qwen3.5 chat-template
thinking by default (the fine-tune was trained without ``<think>`` blocks).
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

# httpx is imported lazily in _get_client(), not here: tier-1 CI installs
# pytest and nothing else, and tests/test_vllm_client.py must import this
# module there. Do not hoist it back to module scope.
if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)


_BASE_URL = os.environ.get("VLLM_BASE_URL", "http://localhost:8000/v1")
_MODEL = os.environ.get("VLLM_MODEL", "qsql-qwen35-9b")
_MAX_TOKENS = int(os.environ.get("VLLM_MAX_TOKENS", "2048"))
_TIMEOUT_S = float(os.environ.get("VLLM_TIMEOUT_S", "120"))
# Sent as "Authorization: Bearer <key>" when set, for endpoints behind an
# authenticating gateway (or vLLM's own --api-key). Unset -> no header, which
# is what a bare local vLLM expects. Never logged.
_API_KEY = os.environ.get("VLLM_API_KEY", "").strip()
# "1" -> draw n candidates as n sequential n=1 requests instead of one n=n
# request. For OpenAI-compatible gateways that reject n>1 (e.g. because they
# meter per completion). Anything else keeps the single n=n request.
_SPLIT_RAW = os.environ.get("MCP_QSQL_SPLIT_REQUESTS", "").strip()
SPLIT_REQUESTS = _SPLIT_RAW == "1"
if _SPLIT_RAW not in ("", "1"):
    log.warning("MCP_QSQL_SPLIT_REQUESTS=%r is not '1'; leaving it off", _SPLIT_RAW)


_client: httpx.AsyncClient | None = None


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if _API_KEY:
        headers["Authorization"] = f"Bearer {_API_KEY}"
    return headers


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        import httpx

        _client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(connect=5.0, read=_TIMEOUT_S, write=10.0, pool=5.0),
            headers=_headers(),
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

    With ``MCP_QSQL_SPLIT_REQUESTS=1`` the ``n`` samples are instead drawn as
    ``n`` sequential ``n=1`` requests (same return shape), for gateways that
    refuse ``n>1``. Slower, and the prompt is prefilled once per request.
    """
    if SPLIT_REQUESTS and n > 1:
        # Sequential, not concurrent: a failure raises exactly as the single
        # request does, and a gateway with a request-rate limit never sees a
        # burst. Each request re-prefills the prompt; the price of this mode.
        out: list[str] = []
        for _ in range(n):
            out.extend(await _post(messages, temperature, top_p, max_tokens, 1))
        return out
    return await _post(messages, temperature, top_p, max_tokens, n)


async def _post(
    messages: list[dict],
    temperature: float,
    top_p: float,
    max_tokens: int | None,
    n: int,
) -> list[str]:
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

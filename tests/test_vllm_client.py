"""Unit tests for ``mcp_server.vllm_client.chat`` request shaping.

The HTTP client is replaced with a fake that records each request body, so
no vLLM, q or pykx is involved. Covers the default single ``n=N`` request and
``MCP_QSQL_SPLIT_REQUESTS=1`` (N sequential ``n=1`` requests, for gateways
that refuse ``n>1``), plus the optional ``VLLM_API_KEY`` header.

Tier 1, so no httpx either: CI installs pytest and nothing else. The fake
response below mimics the two methods ``vllm_client`` calls on an
``httpx.Response`` (``raise_for_status`` and ``json``).
"""

from __future__ import annotations

import asyncio

import pytest

from mcp_server import vllm_client


class _HTTPError(Exception):
    pass


class _FakeResponse:
    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self.payload = payload

    def raise_for_status(self):
        if self.status >= 400:
            raise _HTTPError(self.status)

    def json(self):
        return self.payload


class _FakeClient:
    """Mimics the endpoint: returns ``n`` choices; ``reject_n_gt_1`` models a
    gateway that refuses ``n>1`` outright."""

    def __init__(self, reject_n_gt_1: bool = False, fail_on_call: int | None = None):
        self.bodies: list[dict] = []
        self.reject_n_gt_1 = reject_n_gt_1
        self.fail_on_call = fail_on_call

    async def post(self, url, json):
        self.bodies.append(json)
        if self.reject_n_gt_1 and json["n"] > 1:
            return _FakeResponse(400)
        if self.fail_on_call == len(self.bodies):
            return _FakeResponse(500)
        call = len(self.bodies)
        choices = [
            {"index": i, "message": {"content": f"r{call}c{i}"}}
            for i in range(json["n"])
        ]
        return _FakeResponse(200, {"choices": choices})


def _run(**kw):
    return asyncio.run(vllm_client.chat([{"role": "user", "content": "q"}], **kw))


def _install(monkeypatch, fake, split: bool):
    monkeypatch.setattr(vllm_client, "_get_client", lambda: fake)
    monkeypatch.setattr(vllm_client, "SPLIT_REQUESTS", split)


def test_default_is_one_request_with_n(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake, split=False)
    out = _run(n=4, temperature=0.8)
    assert [b["n"] for b in fake.bodies] == [4]
    assert out == ["r1c0", "r1c1", "r1c2", "r1c3"]


def test_split_sends_n_sequential_single_requests(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake, split=True)
    out = _run(n=4, temperature=0.8)
    assert [b["n"] for b in fake.bodies] == [1, 1, 1, 1]
    # request order == generation order
    assert out == ["r1c0", "r2c0", "r3c0", "r4c0"]


def test_split_survives_gateway_that_refuses_n_gt_1(monkeypatch):
    fake = _FakeClient(reject_n_gt_1=True)
    _install(monkeypatch, fake, split=True)
    assert len(_run(n=8, temperature=0.8)) == 8


def test_default_mode_surfaces_gateway_refusal(monkeypatch):
    fake = _FakeClient(reject_n_gt_1=True)
    _install(monkeypatch, fake, split=False)
    with pytest.raises(_HTTPError):
        _run(n=4, temperature=0.8)


def test_split_failure_raises_like_the_single_request(monkeypatch):
    fake = _FakeClient(fail_on_call=2)
    _install(monkeypatch, fake, split=True)
    with pytest.raises(_HTTPError):
        _run(n=4, temperature=0.8)
    assert len(fake.bodies) == 2  # stops at the failure, no further requests


def test_split_with_n_1_is_unchanged(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake, split=True)
    assert _run(n=1) == ["r1c0"]
    assert [b["n"] for b in fake.bodies] == [1]


def test_no_authorization_header_without_api_key(monkeypatch):
    monkeypatch.setattr(vllm_client, "_API_KEY", "")
    assert "Authorization" not in vllm_client._headers()


def test_api_key_is_sent_as_bearer(monkeypatch):
    monkeypatch.setattr(vllm_client, "_API_KEY", "sk-test")
    assert vllm_client._headers()["Authorization"] == "Bearer sk-test"

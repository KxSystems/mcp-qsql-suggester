"""Unit tests for ``mcp_server.vllm_client.chat`` request shaping.

The HTTP client is replaced with a fake that records each request body, so
no vLLM, q or pykx is involved. Covers the default single ``n=N`` request and
``MCP_QSQL_SPLIT_REQUESTS=1`` (N sequential ``n=1`` requests, for gateways
that refuse ``n>1``) and its ``MCP_QSQL_SPLIT_CONCURRENCY`` variant, the
top-up for endpoints that ignore ``n``, plus the optional ``VLLM_API_KEY``
header.

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

    def __init__(
        self,
        reject_n_gt_1: bool = False,
        fail_on_call: int | None = None,
        ignore_n: bool = False,
    ):
        self.bodies: list[dict] = []
        self.reject_n_gt_1 = reject_n_gt_1
        self.fail_on_call = fail_on_call
        self.ignore_n = ignore_n

    async def post(self, url, json):
        self.bodies.append(json)
        if self.reject_n_gt_1 and json["n"] > 1:
            return _FakeResponse(400)
        if self.fail_on_call == len(self.bodies):
            return _FakeResponse(500)
        call = len(self.bodies)
        choices = [
            {"index": i, "message": {"content": f"r{call}c{i}"}}
            for i in range(1 if self.ignore_n else json["n"])
        ]
        return _FakeResponse(200, {"choices": choices})


def _run(**kw):
    return asyncio.run(vllm_client.chat([{"role": "user", "content": "q"}], **kw))


class _SlowClient(_FakeClient):
    """Yields to the event loop mid-request and records the peak number of
    requests in flight, so concurrency is observable."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.in_flight = 0
        self.peak = 0

    async def post(self, url, json):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            resp = await super().post(url, json)
            await asyncio.sleep(0.01)
            return resp
        finally:
            self.in_flight -= 1


def _install(monkeypatch, fake, split: bool, concurrency: int = 1):
    monkeypatch.setattr(vllm_client, "_get_client", lambda: fake)
    monkeypatch.setattr(vllm_client, "SPLIT_REQUESTS", split)
    monkeypatch.setattr(vllm_client, "SPLIT_CONCURRENCY", concurrency)
    monkeypatch.setattr(vllm_client, "_ignores_n", False)


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


def test_endpoint_ignoring_n_is_topped_up_and_warned(monkeypatch, caplog):
    fake = _FakeClient(ignore_n=True)
    _install(monkeypatch, fake, split=False)
    with caplog.at_level("WARNING", logger=vllm_client.log.name):
        out = _run(n=4, temperature=0.8)
    assert [b["n"] for b in fake.bodies] == [4, 1, 1, 1]
    assert out == ["r1c0", "r2c0", "r3c0", "r4c0"]
    assert "1 of 4 requested choices" in caplog.text


def test_endpoint_ignoring_n_is_remembered(monkeypatch, caplog):
    fake = _FakeClient(ignore_n=True)
    _install(monkeypatch, fake, split=False)
    _run(n=4, temperature=0.8)
    fake.bodies.clear()
    caplog.clear()
    with caplog.at_level("WARNING", logger=vllm_client.log.name):
        assert len(_run(n=8, temperature=0.8)) == 8
    assert [b["n"] for b in fake.bodies] == [1] * 8  # no second short probe
    assert caplog.text == ""  # warned once per process, not per round


def test_request_mode_reports_each_way_of_drawing(monkeypatch):
    fake = _FakeClient(ignore_n=True)
    _install(monkeypatch, fake, split=False)
    assert vllm_client.request_mode() == "n"
    _run(n=4, temperature=0.8)
    assert vllm_client.request_mode() == "split_auto"
    monkeypatch.setattr(vllm_client, "SPLIT_REQUESTS", True)
    assert vllm_client.request_mode() == "split"


def test_full_response_does_not_trip_detection(monkeypatch):
    fake = _FakeClient()
    _install(monkeypatch, fake, split=False)
    _run(n=4, temperature=0.8)
    _run(n=4, temperature=0.8)
    assert [b["n"] for b in fake.bodies] == [4, 4]
    assert vllm_client.request_mode() == "n"


def test_split_is_sequential_by_default(monkeypatch):
    fake = _SlowClient()
    _install(monkeypatch, fake, split=True)
    _run(n=4, temperature=0.8)
    assert fake.peak == 1


def test_split_concurrency_caps_requests_in_flight(monkeypatch):
    fake = _SlowClient()
    _install(monkeypatch, fake, split=True, concurrency=3)
    out = _run(n=8, temperature=0.8)
    assert [b["n"] for b in fake.bodies] == [1] * 8
    assert fake.peak == 3
    # submission order == generation order, as in the sequential loop
    assert out == [f"r{i}c0" for i in range(1, 9)]


def test_split_concurrency_failure_raises_and_cancels_the_rest(monkeypatch):
    fake = _SlowClient(fail_on_call=2)
    _install(monkeypatch, fake, split=True, concurrency=2)
    with pytest.raises(_HTTPError):
        _run(n=8, temperature=0.8)
    assert len(fake.bodies) < 8  # requests still queued were never sent


def test_ignore_n_top_up_uses_split_concurrency(monkeypatch):
    fake = _SlowClient(ignore_n=True)
    _install(monkeypatch, fake, split=False, concurrency=4)
    assert len(_run(n=8, temperature=0.8)) == 8
    assert fake.peak == 4


@pytest.mark.parametrize("raw, want", [
    ("", 1), ("1", 1), ("4", 4), ("0", 1), ("-2", 1), ("four", 1),
])
def test_split_concurrency_parsing(raw, want):
    assert vllm_client._parse_concurrency(raw) == want


def test_no_authorization_header_without_api_key(monkeypatch):
    monkeypatch.setattr(vllm_client, "_API_KEY", "")
    assert "Authorization" not in vllm_client._headers()


def test_api_key_is_sent_as_bearer(monkeypatch):
    monkeypatch.setattr(vllm_client, "_API_KEY", "sk-test")
    assert vllm_client._headers()["Authorization"] == "Bearer sk-test"

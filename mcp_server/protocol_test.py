"""Drive the MCP stdio server as a subprocess and exercise ``tools/call``.

Protocol-layer regression check — verifies that ``mcp_server.server`` boots,
completes the MCP initialize handshake, and dispatches ``tools/call`` to
``generate_qsql`` with a correct return-value round-trip. Complements
``mcp_server.smoke_test`` (which exercises the loop directly without MCP).

Usage::

    QLINT_DIR=<dir containing qlint.q_> conda run --no-capture-output \\
        -n <your-env> python -m mcp_server.protocol_test

Exits 0 on success, 1 on failure. Prints the structured payload and any
server stderr.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
RESPONSE_TIMEOUT_S = 120.0


_MESSAGES = [
    {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "protocol-test", "version": "0.0"},
        },
    },
    {"jsonrpc": "2.0", "method": "notifications/initialized"},
    {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "generate_qsql",
            "arguments": {
                "instruction": "Select the average price per sym from the trade table.",
                "table_schemas": {"trade": "`time`sym`price`size!`t`s`f`j"},
                "max_rounds": 1,
            },
        },
    },
]


async def _drain_stream(stream: asyncio.StreamReader, sink: list[bytes]) -> None:
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            return
        sink.append(chunk)


def _print_payload(payload: dict) -> None:
    print("=== generate_qsql result ===")
    print(f"passed              : {payload.get('passed')}")
    print(f"rounds_used         : {payload.get('rounds_used')}")
    print(f"candidates_generated: {payload.get('candidates_generated')}")
    print(f"winning_cluster_size: {payload.get('winning_cluster_size')}")
    print(f"n_clusters          : {payload.get('n_clusters')}")
    print(f"q_code              : {payload.get('q_code')}")
    print(f"final_issues        : {payload.get('final_issues')}")
    candidates = payload.get("candidates") or []
    print(f"n candidates        : {len(candidates)}")
    if candidates:
        c0 = candidates[0]
        print(
            f"candidate[0]        : parse_ok={c0.get('parse_ok')} "
            f"exec_ran={c0.get('exec_ran')} "
            f"exec_passed={c0.get('exec_passed')} "
            f"exec_err={c0.get('exec_err')!r} "
            f"degenerate={c0.get('degenerate')!r}"
        )


def _extract_payload(result: dict) -> dict | None:
    """FastMCP returns the tool's dict as JSON text under ``content[0].text``
    (no ``structuredContent`` unless the return type is a TypedDict / Pydantic
    model). Handle both shapes.
    """
    if "structuredContent" in result:
        return result["structuredContent"]
    for c in result.get("content", []):
        if c.get("type") == "text":
            try:
                return json.loads(c["text"])
            except (KeyError, json.JSONDecodeError):
                return {"raw_text": c.get("text", "")}
    return None


async def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    if not env.get("QLINT_DIR"):
        print("ERROR: QLINT_DIR not set in environment", file=sys.stderr)
        return 1

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "mcp_server.server",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert proc.stdin and proc.stdout and proc.stderr

    stderr_chunks: list[bytes] = []
    stderr_task = asyncio.create_task(_drain_stream(proc.stderr, stderr_chunks))

    payload_bytes = ("\n".join(json.dumps(m) for m in _MESSAGES) + "\n").encode()
    proc.stdin.write(payload_bytes)
    await proc.stdin.drain()
    # Deliberately do NOT close stdin — closing triggers server shutdown
    # which cancels the in-flight tool call before it can respond.

    rc = 1
    try:
        while True:
            try:
                line = await asyncio.wait_for(
                    proc.stdout.readline(), timeout=RESPONSE_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                print(f"TIMEOUT after {RESPONSE_TIMEOUT_S}s waiting for id=2 response")
                break
            if not line:
                print("Server closed stdout before id=2 response")
                break
            try:
                msg = json.loads(line.decode(errors="replace").strip())
            except json.JSONDecodeError:
                continue
            if msg.get("id") != 2:
                continue
            if "error" in msg:
                print("TOOL ERROR:")
                print(json.dumps(msg["error"], indent=2))
                break
            payload = _extract_payload(msg.get("result", {}))
            if payload is None:
                print("UNEXPECTED result shape:")
                print(json.dumps(msg.get("result", {}), indent=2)[:1500])
                break
            _print_payload(payload)
            rc = 0 if payload.get("passed") else 1
            break
    finally:
        if proc.returncode is None:
            try:
                proc.stdin.close()
            except Exception:
                pass
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        stderr_task.cancel()
        try:
            await stderr_task
        except asyncio.CancelledError:
            pass
        stderr_text = b"".join(stderr_chunks).decode(errors="replace")
        if stderr_text.strip():
            print("--- server stderr ---")
            print(stderr_text, end="")

    return rc


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

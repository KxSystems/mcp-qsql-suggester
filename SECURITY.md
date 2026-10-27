# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately to
[`incubation@kx.com`](mailto:incubation@kx.com), and **not** through a public
GitHub issue. That address is open to everyone, whether or not you are a KX
customer.

If you hold a kdb+ licence and would rather not put the details in email — a
reproduction carrying data you cannot send out, say — raise it through the KX
support portal at [support.kx.com](https://client.support.kx.com/) instead. It is
available to licensed subscribers and is intended as a secure medium for
exchanging sensitive information. It is an alternative for that case, not a
second address to copy in: one report on either channel reaches us.

Include enough to reproduce: the version or commit, how the server was launched
(transport, environment variables), and the request or input that triggers the
behaviour. If you have a proof of concept, a minimal one is more useful than a
complete exploit.

We will acknowledge your report and tell you whether we consider it in scope. We
do not operate a bug bounty and cannot offer payment.

## What this software does by design

Read this before reporting, because the single most important property of this
server is also the one most likely to look like a vulnerability.

**The server executes model-generated q code in the process that serves your
requests.** That is its purpose, not a flaw: candidates drafted by the language
model are validated by running them against a dummy table built from the
`table_schemas` you supply. Execution is protected by a q timeout
(`system "T 3"`) and errors are trapped (`@[value;code;{"ERR:",x}]`), and that is
the whole of the protection. There is **no sandbox**. Embedded q has the full
authority of the server process: the filesystem it can reach, the network it can
open, the environment it can read.

Two consequences follow, and both are deliberate:

- **A caller who can reach the endpoint can, in practice, cause arbitrary q to
  run.** The executed code is drafted by a model from the caller's own
  instruction and schemas, so a caller who shapes the instruction shapes the
  code. Treat access to this endpoint as equivalent to shell access on the host,
  and grant it on that basis.
- **The MCP server performs no authentication or authorisation of its own, and
  terminates no TLS.** In `streamable-http` mode it binds `0.0.0.0:9000` at path
  `/mcp` by default; `MCP_QSQL_HOST=127.0.0.1` restricts it to loopback.

The supported deployment is therefore: **loopback, an SSH tunnel, or behind a
reverse proxy that authenticates**, on a host whose other users you trust, and
reachable only by callers you would trust with a shell there. Running it on an
untrusted network is a deployment error rather than a defect in the software.

### Out of scope

Reports that amount to the following are documented behaviour, and we will close
them as such:

- The endpoint has no authentication (see above).
- Model-generated q executes without a sandbox (see above).
- A prompt can be crafted so that the generated q does something destructive.
  This is the same finding as the two above.
- `passed=true` in a response does not prove the returned q is semantically
  correct. This is stated in the README and the model card: it means the code
  ran without raising and independent samples agreed, which is not a proof of
  intent.
- Denial of service by an authorised caller — for example many concurrent
  requests, or a schema that makes dummy-table generation expensive. Callers are
  assumed to be trusted.

### In scope

Things we do want to hear about:

- A way to make the server execute q that a **caller could not have caused
  through the documented request path** — for instance code injected via a
  response from the vLLM backend, or via a field that is supposed to be treated
  as data.
- Escape from the gates in a way that is not the caller's own doing: candidate
  code reaching execution while bypassing the parse gate, or the execution
  timeout failing to bound a candidate.
- Leakage between concurrent callers of one server: one caller's schemas,
  instruction, or generated code becoming visible to another. (Note the known
  case, documented in `CLAUDE.md`: `MCP_QSQL_RETRIEVE_EXPECTED_SCHEMA` must be
  left unset on a shared HTTP server, because the environment-variable fallback
  it enables is frozen at process start and would apply one caller's shape hint
  to every request. Turning it on against that instruction is a
  misconfiguration; a way to leak *without* it is a vulnerability.)
- Anything that lets a request read or write files outside what the documented
  execution path needs, or that persists state between requests in a way the
  design does not intend.
- Vulnerabilities in this repository's own code — the MCP server, the shared
  helpers, or `tools/prepare_release.py`.

Vulnerabilities in our dependencies — pykx, kdb+/q, vLLM, the model weights,
`mcp`, Starlette — should go to those projects. Tell us as well if this server's
use of them makes the impact worse than it would otherwise be, and we will
adjust our own code or documentation.

## Supported versions

This project has no released versions yet. Fixes land on the default branch, and
that is the only version we support. If you are running an older commit, expect
to update rather than to receive a backport.

## Handling of licensed third-party artefacts

kdb+/q, pykx and KX's `qlint.q_` are licensed separately and are **not**
redistributed here; you supply your own copies. Do not attach them to a report.
If a report requires one to reproduce, describe the version instead.

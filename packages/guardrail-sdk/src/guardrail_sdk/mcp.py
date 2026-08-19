"""An MCP proxy that governs a server which knows nothing about Guardrail.

The Model Context Protocol is how a growing number of agents reach their tools: the
client speaks JSON-RPC to a server, and `tools/call` is the moment an action happens.
That makes it the perfect interception point, and it is the one place where governance
can be added to a *third-party* tool server with **zero changes to it**.

    agent ──stdio──► guardrail-mcp ──stdio──► @modelcontextprotocol/server-filesystem
                          │
                          └── POST /v1/evaluate  (before any tools/call is forwarded)

Everything except `tools/call` is forwarded untouched, in both directions. That matters
more than it sounds: MCP is still moving, and a proxy that only understood the methods
it was written against would silently break a server that added one. Unknown methods,
notifications, and future capabilities all pass straight through.

## What it means for the protocol when policy says no

A refused call comes back as a **successful JSON-RPC response whose result carries
`isError: true`**, not as a JSON-RPC error. That is deliberate and it follows the MCP
convention: a tool-level failure is content the model is supposed to read and reason
about, whereas a protocol error reads as "the server is broken" and typically aborts the
session. The agent should be told *why* it was refused and which rule did it, and then
carry on -- refusing an action is not the same as crashing.

## Ordering

The upstream server never sees a blocked call. The evaluation happens before the message
is forwarded, so this is enforcement rather than detection. That is the whole point: a
proxy that forwarded first and evaluated afterwards would be an audit log with extra
latency.

## The trust boundary, stated plainly

This governs traffic that goes *through* the proxy. An agent that can also reach the
upstream server directly is not governed by it, exactly as a network proxy is bypassed by
a direct connection. The proxy is the enforcement point for the path it owns; making it
the *only* path is a deployment concern -- run the upstream over stdio as a child of this
process, as `stdio_main` does, and there is no other path to it.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import IO, Any

from guardrail_sdk.client import GuardrailClient
from guardrail_sdk.exceptions import GuardrailUnavailable

JsonRpc = dict[str, Any]

TOOLS_CALL = "tools/call"


@dataclass
class ProxyDecision:
    """What the proxy decided to do with one client message."""

    forward: JsonRpc | None = None
    """The message to send upstream, or None when it was intercepted."""

    respond: JsonRpc | None = None
    """A response to return to the client without involving upstream."""


@dataclass
class ProxyStats:
    """Counters, so a demo can show what the proxy actually did."""

    forwarded: int = 0
    evaluated: int = 0
    blocked: int = 0
    held: int = 0
    allowed: int = 0
    errors: int = 0
    blocked_tools: list[str] = field(default_factory=list)


class MCPGuardrailProxy:
    """The pure protocol logic: message in, decision out.

    Deliberately free of pipes and subprocesses so it can be tested against crafted
    JSON-RPC without spawning anything. `stdio_main` wires it to real streams.
    """

    def __init__(
        self,
        client: GuardrailClient,
        *,
        server_name: str = "mcp",
        agent_id: str = "mcp-agent",
        session_id: str | None = None,
        on_pending: str = "block",
        fail_open: bool = False,
        context: dict[str, Any] | None = None,
    ) -> None:
        """`server_name` prefixes every tool so policies can address it.

        An upstream `read_file` becomes `filesystem.read_file`, which is what lets one
        bundle distinguish two servers that happen to expose the same tool name. Without
        it, a rule written for one server would silently govern the other.
        """
        self.client = client
        self.server_name = server_name
        self.agent_id = agent_id
        self.session_id = session_id or f"mcp-{uuid.uuid4().hex[:12]}"
        self.on_pending = on_pending
        self.fail_open = fail_open
        self.context = context or {}
        self.stats = ProxyStats()

    # ------------------------------------------------------------------
    def qualified_tool(self, name: str) -> str:
        """`read_file` -> `filesystem.read_file`."""
        return f"{self.server_name}.{name}" if self.server_name else name

    def handle_client_message(self, message: JsonRpc) -> ProxyDecision:
        """Decide what happens to one message travelling client -> server."""
        if message.get("method") != TOOLS_CALL:
            # Everything else is none of our business: initialize, tools/list,
            # resources/*, prompts/*, notifications, and anything MCP adds later.
            self.stats.forwarded += 1
            return ProxyDecision(forward=message)

        params = message.get("params") or {}
        tool_name = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            # A server could accept non-object arguments. Wrap rather than reject, so
            # the policy still sees something addressable instead of the proxy 500ing.
            arguments = {"value": arguments}

        self.stats.evaluated += 1

        try:
            decision = self.client.evaluate(
                tool=self.qualified_tool(tool_name),
                arguments=arguments,
                agent_id=self.agent_id,
                session_id=self.session_id,
                context={**self.context, "transport": "mcp", "mcp_server": self.server_name},
                idempotency_key=str(uuid.uuid4()),
            )
        except GuardrailUnavailable as exc:
            self.stats.errors += 1
            if self.fail_open:
                # Explicit, logged, and never the default: an operator chose availability
                # over governance for this proxy.
                self.stats.forwarded += 1
                _warn(f"guardrail unavailable, failing OPEN: {exc}")
                return ProxyDecision(forward=message)

            self.stats.blocked += 1
            self.stats.blocked_tools.append(tool_name)
            _warn(f"guardrail unavailable, failing closed: {exc}")
            return ProxyDecision(
                respond=_tool_error(
                    message.get("id"),
                    f"REFUSED: the guardrail could not be reached, so the action was not "
                    f"performed. ({exc}) This proxy fails closed by design.",
                )
            )

        if decision.decision == "block":
            self.stats.blocked += 1
            self.stats.blocked_tools.append(tool_name)
            return ProxyDecision(respond=_tool_error(message.get("id"), _refusal(decision)))

        if decision.decision == "require_hitl":
            return self._handle_pending(message, decision)

        self.stats.allowed += 1
        self.stats.forwarded += 1
        return ProxyDecision(forward=message)

    # ------------------------------------------------------------------
    def _handle_pending(self, message: JsonRpc, decision: Any) -> ProxyDecision:
        """A call a human has to approve.

        `block` is the default rather than `wait`, because an MCP client is usually
        interactive and a proxy that silently stops responding for fifteen minutes looks
        indistinguishable from a hang. Telling the agent an approval is outstanding lets
        it say so and move on. `wait` is right for a batch host and is one flag away.
        """
        self.stats.held += 1

        if self.on_pending == "wait":
            try:
                final = self.client.wait_for_decision(decision.decision_id)
            except (GuardrailUnavailable, Exception) as exc:
                self.stats.errors += 1
                return ProxyDecision(
                    respond=_tool_error(
                        message.get("id"),
                        f"REFUSED: waiting for human review failed ({exc}), so the action "
                        "was not performed.",
                    )
                )

            if final.allows_execution:
                self.stats.allowed += 1
                self.stats.forwarded += 1
                return ProxyDecision(forward=message)

            reviewer = f" by {final.reviewer}" if final.reviewer else ""
            reason = f": {final.reason}" if final.reason else ""
            return ProxyDecision(
                respond=_tool_error(
                    message.get("id"),
                    f"REFUSED: human review {final.status}{reviewer}{reason}. "
                    "The action was not performed.",
                )
            )

        return ProxyDecision(
            respond=_tool_error(
                message.get("id"),
                f"HELD FOR HUMAN REVIEW: {_refusal(decision)} "
                f"Decision id {decision.decision_id}. The action was not performed. "
                "Tell the user approval is pending rather than retrying.",
            )
        )


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def _refusal(decision: Any) -> str:
    """The sentence the model reads.

    Names the rule on purpose. "That's not allowed" invites a retry loop; "rule
    db-bulk-delete blocks deletions over 100 rows" tells the agent what to do instead.
    """
    rules = ", ".join(getattr(decision, "rule_ids", []) or [])
    base = decision.message or f"The action was {decision.decision}."
    return f"{base}" + (f" (policy: {rules})" if rules else "")


def _tool_error(request_id: Any, text: str) -> JsonRpc:
    """An MCP tool-level failure.

    A *result* with `isError: true`, not a JSON-RPC error. MCP clients surface the former
    to the model as readable content and the latter as a broken server, and a refusal is
    something the agent should understand rather than choke on.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


def _warn(text: str) -> None:
    """Diagnostics go to stderr.

    stdout is the JSON-RPC channel; one stray print there corrupts the protocol stream
    and the client disconnects with a parse error that looks nothing like its cause.
    """
    print(f"[guardrail-mcp] {text}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# stdio transport
# ---------------------------------------------------------------------------


def _pump(
    source: IO[str],
    sink: IO[str],
    transform: Callable[[str], str | None],
    *,
    on_eof: Callable[[], None] | None = None,
) -> None:
    """Move lines from one stream to another until the source closes.

    **`readline()`, never `for line in source`.** Python's file iterator reads ahead in
    blocks, so on a pipe it sits waiting for the buffer to fill before yielding the first
    line -- and JSON-RPC is a request/response protocol where nothing more arrives until
    the current message is answered. Iterating deadlocks the session at `initialize` with
    no error, which looks exactly like a hung server. `readline()` returns as soon as a
    complete line exists.

    `transform` may return None to swallow a line. Exceptions are reported and the line
    passed through rather than killing the thread: a dead pump is indistinguishable from
    a hung server, which is the most confusing failure a proxy can have.

    `on_eof` runs when the source closes, so the far end can be shut down in turn.
    Without it the upstream child never sees EOF and never exits.
    """
    try:
        while True:
            line = source.readline()
            if not line:
                break
            if not line.strip():
                continue
            try:
                out = transform(line)
            except Exception as exc:  # a bad line must not kill the pipe
                _warn(f"error handling message, passing it through: {exc}")
                out = line
            if out is not None:
                try:
                    sink.write(out if out.endswith(chr(10)) else out + chr(10))
                    sink.flush()
                except (BrokenPipeError, ValueError, OSError):
                    break
    except (BrokenPipeError, ValueError, OSError):
        pass  # The other end went away; shutting down is the correct response.
    finally:
        if on_eof is not None:
            with contextlib.suppress(Exception):
                on_eof()


def resolve_executable(name: str) -> str:
    """Find a runnable path for a command, correctly on Windows.

    Most MCP servers ship as npm packages and are launched with `npx`, and on Windows
    `npx` exists twice: a `.cmd` shim that Windows can execute, and an extensionless shell
    script for Git Bash that it cannot. `shutil.which` happily returns the latter, and
    `Popen` then fails with `WinError 193: %1 is not a valid Win32 application` -- an error
    that says nothing about the actual problem.

    So on Windows the PATHEXT variants are preferred explicitly. Elsewhere this is just
    `shutil.which` with the original string as a fallback.
    """
    import os
    import shutil

    if os.name == "nt":
        extensions = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        for extension in extensions:
            found = shutil.which(name + extension.lower()) or shutil.which(name + extension)
            if found:
                return found

    return shutil.which(name) or name


def stdio_main(argv: list[str] | None = None) -> int:
    """Run the proxy over stdio, with the upstream server as a child process.

        guardrail-mcp --server filesystem -- npx -y @modelcontextprotocol/server-filesystem /tmp

    Running upstream as our own child is what makes the proxy the only path to it: the
    agent holds a pipe to us, and nothing else holds one to the server.
    """
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="guardrail-mcp",
        description="Enforce Guardrail policy on every tools/call of any MCP server.",
    )
    parser.add_argument(
        "--server",
        default="mcp",
        help="Name prefixing every tool in policy, e.g. --server filesystem makes "
        "read_file address as filesystem.read_file.",
    )
    parser.add_argument(
        "--endpoint", default="", help="Guardrail base URL (or GUARDRAIL_BASE_URL)."
    )
    parser.add_argument("--api-key", default="", help="Guardrail API key (or GUARDRAIL_API_KEY).")
    parser.add_argument("--agent-id", default="mcp-agent")
    parser.add_argument(
        "--on-pending",
        choices=["block", "wait"],
        default="block",
        help="What a require_hitl decision does. `block` tells the agent approval is "
        "pending; `wait` blocks until a human answers (right for batch hosts).",
    )
    parser.add_argument(
        "--fail-open",
        action="store_true",
        help="Forward calls when the guardrail is unreachable. Off by default, and an "
        "explicit, logged choice when on.",
    )
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="-- followed by the upstream MCP server command.",
    )
    args = parser.parse_args(argv)

    command = [c for c in args.command if c != "--"]
    if not command:
        parser.error("no upstream server command given; put it after --")

    client = GuardrailClient(
        base_url=args.endpoint or os.environ.get("GUARDRAIL_BASE_URL", ""),
        api_key=args.api_key or os.environ.get("GUARDRAIL_API_KEY", ""),
        fail_open=args.fail_open,
    )
    proxy = MCPGuardrailProxy(
        client,
        server_name=args.server,
        agent_id=args.agent_id,
        on_pending=args.on_pending,
        fail_open=args.fail_open,
    )

    _warn(f"governing {' '.join(command)} as '{args.server}.*' via {client.base_url}")

    # Resolve argv[0] before spawning, so a Windows shim is used rather than the shell
    # script sitting next to it. See resolve_executable.
    command = [resolve_executable(command[0]), *command[1:]]

    upstream = subprocess.Popen(  # noqa: S603 - the command is operator-supplied, by design
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=None,  # let the child's diagnostics reach the operator untouched
        text=True,
        bufsize=1,
    )
    assert upstream.stdin is not None and upstream.stdout is not None

    def from_client(line: str) -> str | None:
        message = json.loads(line)
        decision = proxy.handle_client_message(message)

        if decision.respond is not None:
            # Answer the client directly. Upstream never learns the call was attempted,
            # which is the difference between enforcement and a log line.
            sys.stdout.write(json.dumps(decision.respond) + "\n")
            sys.stdout.flush()
            return None

        return json.dumps(decision.forward) if decision.forward is not None else None

    def close_upstream_stdin() -> None:
        """The client hung up, so tell the server. Without this the child waits forever
        for input that will never come and the process never exits."""
        assert upstream.stdin is not None
        with contextlib.suppress(BrokenPipeError, ValueError, OSError):
            upstream.stdin.close()

    reader = threading.Thread(
        target=_pump,
        args=(sys.stdin, upstream.stdin, from_client),
        kwargs={"on_eof": close_upstream_stdin},
        daemon=True,
    )
    writer = threading.Thread(
        target=_pump, args=(upstream.stdout, sys.stdout, lambda line: line), daemon=True
    )
    reader.start()
    writer.start()

    code = upstream.wait()
    # Drain whatever the server wrote just before exiting, or the last response can be
    # lost -- which in a request/response protocol looks like a dropped reply.
    writer.join(timeout=5)

    s = proxy.stats
    _warn(
        f"session over: {s.evaluated} evaluated, {s.allowed} allowed, {s.blocked} blocked, "
        f"{s.held} held for review"
        + (f" (blocked: {', '.join(sorted(set(s.blocked_tools)))})" if s.blocked_tools else "")
    )
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(stdio_main())

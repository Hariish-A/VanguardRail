"""End-to-end proof: Guardrail governs an off-the-shelf MCP server.

Runs the **real** `@modelcontextprotocol/server-filesystem` behind `guardrail-mcp`, speaks
real JSON-RPC to the proxy, and checks what came back.

    uv run python scripts/mcp_demo.py

The server is unmodified and knows nothing about Guardrail. It is fetched by `npx` at run
time, so this exercises the published package rather than a stub.

## What makes this proof rather than demonstration

A canary string is written into the fake private key. If the blocked read had reached the
server, the key's contents would have come back through the proxy and the canary would
appear in the transcript. Asserting the canary is **absent** is what distinguishes
enforcement from an audit log: a proxy that forwarded first and logged afterwards would
still report `block`, and every other check here would still pass.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

CANARY = "CANARY-a7f3c1-IF-YOU-SEE-THIS-THE-KEY-LEAKED"
BASE_URL = "https://y5ycfqeeilb24ylgmsse2agl5i0njovv.lambda-url.us-east-1.on.aws"


def build_workspace(root: Path) -> None:
    """A directory the MCP server is allowed to serve, with one safe and one secret file."""
    (root / "report.txt").write_text("Q3 revenue was up 4%.\n", encoding="utf-8")
    ssh = root / ".ssh"
    ssh.mkdir(exist_ok=True)
    ssh.joinpath("id_rsa").write_text(
        f"-----BEGIN OPENSSH PRIVATE KEY-----\n{CANARY}\n-----END OPENSSH PRIVATE KEY-----\n",
        encoding="utf-8",
    )


def rpc(request_id: int, method: str, params: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return json.dumps(body) + "\n"


def main() -> int:
    api_key = os.environ.get("GUARDRAIL_API_KEY", "")
    if not api_key:
        print("Set GUARDRAIL_API_KEY (source .env) before running this.", file=sys.stderr)
        return 2

    # Let the proxy resolve it: on Windows the runnable form is npx.cmd, and picking the
    # wrong one fails with WinError 193 rather than anything informative.
    npx = "npx"
    if not (shutil.which("npx") or shutil.which("npx.cmd")):
        print("npx not found; this demo runs the real MCP filesystem server.", file=sys.stderr)
        return 2

    workspace = Path(tempfile.mkdtemp(prefix="guardrail-mcp-"))
    build_workspace(workspace)
    print(f"workspace: {workspace}")
    print(f"guardrail: {BASE_URL}\n")

    # guardrail-mcp launches the upstream server as its own child, so the only path to
    # the filesystem server is through the proxy.
    command = [
        sys.executable,
        "-m",
        "guardrail_sdk.mcp",
        "--server",
        "filesystem",
        "--endpoint",
        BASE_URL,
        "--api-key",
        api_key,
        "--agent-id",
        "mcp-demo",
        "--",
        npx,
        "--yes",
        "@modelcontextprotocol/server-filesystem",
        str(workspace),
    ]

    proxy = subprocess.Popen(  # noqa: S603 - fixed command, built above
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proxy.stdin and proxy.stdout and proxy.stderr

    script = "".join(
        [
            rpc(
                1,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "guardrail-demo", "version": "1.0"},
                },
            ),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n",
            rpc(2, "tools/list"),
            # Permitted: an ordinary file. Proves governance is not obstruction.
            rpc(
                3,
                "tools/call",
                {"name": "read_text_file", "arguments": {"path": str(workspace / "report.txt")}},
            ),
            # Refused: a private key. The server must never see this request.
            rpc(
                4,
                "tools/call",
                {
                    "name": "read_text_file",
                    "arguments": {"path": str(workspace / ".ssh" / "id_rsa")},
                },
            ),
            # Refused: a write. Held for a human.
            rpc(
                5,
                "tools/call",
                {
                    "name": "write_file",
                    "arguments": {"path": str(workspace / "report.txt"), "content": "overwritten"},
                },
            ),
        ]
    )

    try:
        stdout, stderr = proxy.communicate(script, timeout=180)
    except subprocess.TimeoutExpired:
        proxy.kill()
        stdout, stderr = proxy.communicate()

    responses: dict[int, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(message.get("id"), int):
            responses[message["id"]] = message

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{'  -- ' + detail if detail else ''}")
        if not ok:
            failures.append(label)

    print("=== the server is real and unmodified ===")
    init = responses.get(1, {}).get("result", {})
    server_info = init.get("serverInfo", {})
    check(
        "initialize handled by the upstream server",
        bool(server_info),
        f"{server_info.get('name')} {server_info.get('version')}",
    )
    tools = responses.get(2, {}).get("result", {}).get("tools", [])
    check("tools/list passed through untouched", len(tools) > 0, f"{len(tools)} tools exposed")

    print("\n=== governance is not obstruction ===")
    allowed = responses.get(3, {}).get("result", {})
    allowed_text = json.dumps(allowed)
    check("ordinary read succeeded", allowed.get("isError") is not True)
    check("and returned the file's real content", "Q3 revenue" in allowed_text)

    print("\n=== the secret read was refused ===")
    blocked = responses.get(4, {}).get("result", {})
    blocked_text = json.dumps(blocked)
    check("refused", blocked.get("isError") is True)
    check("named the rule that did it", "mcp-credential-read-block" in blocked_text)
    check("returned as a result, not a protocol error", "error" not in responses.get(4, {}))

    print("\n=== the write was held for a human ===")
    held = responses.get(5, {}).get("result", {})
    check("held", held.get("isError") is True)
    check("said so", "HELD FOR HUMAN REVIEW" in json.dumps(held))
    check(
        "and the file on disk is untouched",
        (workspace / "report.txt").read_text(encoding="utf-8").startswith("Q3 revenue"),
    )

    print("\n=== THE PROOF: the upstream server never saw the blocked request ===")
    check(
        "the private key's contents never crossed the proxy",
        CANARY not in stdout,
        "canary absent from the entire transcript",
    )

    print("\n--- proxy diagnostics ---")
    for line in stderr.splitlines():
        if "[guardrail-mcp]" in line:
            print("  " + line.strip())

    shutil.rmtree(workspace, ignore_errors=True)

    print()
    if failures:
        print(f"FAILURES: {failures}")
        return 1
    print("ALL CHECKS PASSED -- an unmodified MCP server was governed end to end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Load test for `/v1/evaluate`, sized against the capacity this account actually has.

    uv run python scripts/loadtest.py --endpoint "$BASE" --api-key "$GUARDRAIL_API_KEY"

k6 would be the conventional choice and the plan named it. Python is used instead for one
reason worth stating: k6 is another toolchain to install, and this measurement needs to be
reproducible by anyone reading the report with nothing but the repo. The work here is
entirely I/O-bound, so threads are more than adequate -- the client is not the bottleneck,
and the script proves that by reporting its own send rate alongside the latencies.

## What is deliberately NOT measured

**No LLM is involved.** The enforcement path never calls one, so including inference would
measure Ollama rather than the guardrail and would make the numbers meaningless.

## The ceiling, stated rather than implied

The audit table is provisioned at **5 WCU**, and every evaluation writes exactly one audit
record. So sustained throughput is bounded at roughly **5 writes per second** before
DynamoDB throttles -- and Lambda reserved concurrency caps containers at 10. Those are the
real numbers, and a report that quoted a burst figure without them would be describing a
different system.

Throttling here is **correct behaviour, not failure**: the free tier is a deliberate
choice, and the same stack scales by changing one CDK constant once a budget exists.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx

SCENARIOS: list[tuple[str, dict[str, Any]]] = [
    ("db.delete_records", {"table": "users", "count": 5}),
    ("db.delete_records", {"table": "users", "count": 500}),
    ("file.read", {"path": "/srv/confidential/q3.pdf"}),
    ("email.send", {"to": ["bob@acme-corp.com"], "subject": "hi"}),
    ("http.request", {"method": "GET", "url": "https://status.acme-corp.com"}),
]


@dataclass
class Results:
    latencies: list[float] = field(default_factory=list)
    engine_latencies: list[float] = field(default_factory=list)
    statuses: dict[int, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, status: int, elapsed_ms: float, engine_ms: float | None) -> None:
        with self.lock:
            self.latencies.append(elapsed_ms)
            self.statuses[status] = self.statuses.get(status, 0) + 1
            if engine_ms is not None:
                self.engine_latencies.append(engine_ms)

    def fail(self, message: str) -> None:
        with self.lock:
            self.errors.append(message)


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile. Explicit rather than a library call so the report's
    numbers can be reproduced by hand from the raw latencies."""
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round(p / 100 * len(ordered) + 0.5)) - 1))
    return ordered[index]


def worker(client: httpx.Client, results: Results, deadline: float, index: int) -> None:
    n = index
    while time.monotonic() < deadline:
        tool, arguments = SCENARIOS[n % len(SCENARIOS)]
        n += 1
        payload = {
            "agent_id": "loadtest",
            "session_id": f"load-{index}",
            "tool": tool,
            "arguments": arguments,
            # A fresh key per request. Reusing one would return the cached decision and
            # measure the idempotency lookup instead of the evaluation path.
            "idempotency_key": str(uuid.uuid4()),
        }

        started = time.perf_counter()
        try:
            response = client.post("/v1/evaluate", json=payload)
        except httpx.HTTPError as exc:
            results.fail(f"{type(exc).__name__}: {exc}")
            continue

        elapsed = (time.perf_counter() - started) * 1000
        engine = None
        if response.status_code == 200:
            try:
                engine = float(response.json().get("latency_ms", 0.0))
            except (json.JSONDecodeError, ValueError, TypeError):
                engine = None

        results.record(response.status_code, elapsed, engine)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", default=os.environ.get("GUARDRAIL_BASE_URL", ""))
    parser.add_argument("--api-key", default=os.environ.get("GUARDRAIL_API_KEY", ""))
    parser.add_argument("--concurrency", type=int, default=10)
    parser.add_argument("--duration", type=float, default=30.0, help="seconds")
    parser.add_argument("--report", default="", help="write a markdown report here")
    args = parser.parse_args(argv)

    if not args.endpoint or not args.api_key:
        print("need --endpoint and --api-key (or the env vars)", file=sys.stderr)
        return 2

    base = args.endpoint.rstrip("/")
    results = Results()

    print(f"target      : {base}")
    print(f"concurrency : {args.concurrency}")
    print(f"duration    : {args.duration}s")
    print("\nrunning...", flush=True)

    started_at = time.monotonic()
    deadline = started_at + args.duration

    with httpx.Client(
        base_url=base,
        timeout=30.0,
        headers={"x-api-key": args.api_key, "content-type": "application/json"},
        limits=httpx.Limits(
            max_connections=args.concurrency * 2,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            for i in range(args.concurrency):
                pool.submit(worker, client, results, deadline, i)

    wall = time.monotonic() - started_at
    total = sum(results.statuses.values())
    ok = results.statuses.get(200, 0)
    throttled = results.statuses.get(429, 0)
    server_errors = sum(c for s, c in results.statuses.items() if s >= 500)

    lines = [
        "",
        "=" * 62,
        "GUARDRAIL LOAD TEST",
        "=" * 62,
        f"  target          : {base}",
        f"  concurrency     : {args.concurrency}",
        f"  wall time       : {wall:.1f}s",
        f"  requests        : {total}  ({total / wall:.1f}/s sustained)",
        f"  200 OK          : {ok}",
        f"  429 throttled   : {throttled}",
        f"  5xx             : {server_errors}",
        f"  transport errors: {len(results.errors)}",
        "",
        "  Round-trip latency (client observed, includes network + Lambda)",
        f"    p50 : {percentile(results.latencies, 50):8.1f} ms",
        f"    p95 : {percentile(results.latencies, 95):8.1f} ms",
        f"    p99 : {percentile(results.latencies, 99):8.1f} ms",
        f"    max : {max(results.latencies, default=0.0):8.1f} ms",
    ]

    if results.engine_latencies:
        lines += [
            "",
            "  Policy engine only (server-measured, excludes network and cold start)",
            f"    p50 : {percentile(results.engine_latencies, 50):8.3f} ms",
            f"    p95 : {percentile(results.engine_latencies, 95):8.3f} ms",
            f"    p99 : {percentile(results.engine_latencies, 99):8.3f} ms",
            f"    mean: {statistics.mean(results.engine_latencies):8.3f} ms",
        ]

    lines += [
        "",
        "  The ceiling this ran against",
        "    DynamoDB      : 5 provisioned WCU -- one audit write per evaluation, so",
        "                    sustained throughput is bounded near 5 writes/second.",
        "    Lambda        : reserved concurrency 10.",
        "    Rate limiter  : 600/min per tenant per container.",
        "",
        "    Throttling is correct behaviour here, not failure. The free tier is a",
        "    deliberate constraint; the same stack scales by changing one CDK constant.",
        "=" * 62,
        "",
    ]

    report = "\n".join(lines)
    print(report)

    if args.report:
        from pathlib import Path

        out = Path(args.report)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "# Guardrail load test\n\n```\n" + report.strip() + "\n```\n", encoding="utf-8"
        )
        print(f"wrote {out}")

    # 5xx and transport errors are real failures. 429 is the limiter working.
    return 1 if (server_errors or results.errors) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Prove the control plane is not Lambda-locked.

    uv run python scripts/portability_proof.py

Builds the image, runs it as a plain container, and executes the **same** conformance
suite that runs against deployed AWS. An identical green report is the evidence: the
FastAPI app, the policy engine, and every rule behave the same on ECS, EKS, Cloud Run, or
on-prem Kubernetes, because only the entrypoint differs (uvicorn here, Mangum on Lambda).

This matters for the "could be adopted by an enterprise without significant rework"
question. A control plane that only runs on one vendor's serverless product is a much
harder thing to adopt than one that happens to be deployed there.

## Why this script exists rather than a paragraph claiming it

The Dockerfile asserted portability from M0 and **had never been built and run**. When it
finally was, in M5, the image did not start: `uv` had installed `guardrail-core` as an
editable, leaving site-packages with a `.pth` pointing at a build-stage path that does not
exist at runtime, so the container died at import. The claim had been false for four
milestones and nothing noticed, because nothing checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

CONTAINER = "guardrail-portability-proof"
API_KEY = "portability-proof-key"
PORT = 8099


def run(*command: str, check: bool = True, quiet: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(command, capture_output=True, text=True)  # noqa: S603
    if check and result.returncode != 0:
        if not quiet:
            print(result.stdout[-2000:])
            print(result.stderr[-2000:], file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(command)}")
    return result


def wait_for_health(url: str, timeout: float = 60.0) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            # Fixed localhost URL built in this file; not attacker-influenced.
            with urllib.request.urlopen(url, timeout=3) as response:  # noqa: S310
                body: dict[str, object] = json.loads(response.read())
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(1.5)
    raise SystemExit(f"container never became healthy: {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true", help="leave the container running")
    parser.add_argument("--skip-build", action="store_true")
    args = parser.parse_args(argv)

    if not args.skip_build:
        print("building guardrail:latest ...", flush=True)
        run("docker", "build", "-t", "guardrail:latest", ".")

    key_hash = hashlib.sha256(API_KEY.encode()).hexdigest()
    keys = json.dumps(
        {key_hash: {"key_id": "portability", "tenant_id": "acme", "name": "portability proof"}}
    )

    run("docker", "rm", "-f", CONTAINER, check=False, quiet=True)
    print(f"starting {CONTAINER} on :{PORT} ...", flush=True)
    run(
        "docker",
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        f"{PORT}:8080",
        "-e",
        "GUARDRAIL_STAGE=local",
        "-e",
        "GUARDRAIL_VERSION=portability-proof",
        # Off so the suite's rapid-fire requests are not throttled; rate limiting has its
        # own tests and is not what this script is measuring.
        "-e",
        "GUARDRAIL_RATE_LIMIT_PER_MINUTE=0",
        "-e",
        f"GUARDRAIL_API_KEYS_JSON={keys}",
        "guardrail:latest",
    )

    try:
        health = wait_for_health(f"http://localhost:{PORT}/healthz")
        print(f"  healthy: {health}\n")

        print("running the conformance suite against the container ...", flush=True)
        result = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "guardrail_sim.cli",
                "run",
                "scenarios/",
                "--endpoint",
                f"http://localhost:{PORT}",
                "--api-key",
                API_KEY,
            ],
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        if result.stderr.strip():
            print(result.stderr, file=sys.stderr)

        if result.returncode != 0:
            print("PORTABILITY PROOF FAILED: the container did not behave like the deploy")
            return 1

        print("PORTABILITY PROOF PASSED")
        print("  The same scenarios that pass against deployed AWS Lambda pass against a")
        print("  plain container, with no code change -- only the entrypoint differs.")
        return 0
    finally:
        if args.keep:
            print(f"\n(container {CONTAINER} left running on :{PORT})")
        else:
            run("docker", "rm", "-f", CONTAINER, check=False, quiet=True)


if __name__ == "__main__":
    raise SystemExit(main())

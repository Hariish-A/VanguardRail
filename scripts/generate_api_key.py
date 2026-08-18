"""Mint an API key for a tenant.

The key itself is printed once and never stored. Only its SHA-256 hash goes into the
deployment, so nothing in the repository, the CloudFormation template, or the Lambda
environment discloses a usable credential.

    uv run python scripts/generate_api_key.py --tenant acme --name "demo agent"

Prints the key to copy, and the JSON to pass as GUARDRAIL_API_KEYS_JSON at deploy time.
To keep existing keys working, pass the current JSON with --merge.

M5 replaces this with a DynamoDB-backed key store so keys can be issued and revoked
without a redeploy. Until then, adding a key means redeploying -- which is acceptable
while the set of callers is small and known.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys

KEY_BYTES = 32
"""256 bits of entropy. Long enough that guessing is not a threat model."""


def mint(tenant_id: str, name: str, key_id: str | None = None) -> tuple[str, dict[str, object]]:
    """Return (plaintext key, entry keyed by its hash)."""
    raw = f"gr_{secrets.token_urlsafe(KEY_BYTES)}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    entry = {
        digest: {
            "key_id": key_id or f"{tenant_id}-{secrets.token_hex(4)}",
            "tenant_id": tenant_id,
            "name": name,
        }
    }
    return raw, entry


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant", default="default", help="tenant this key authenticates as")
    parser.add_argument("--name", default="unnamed", help="human label, shown in logs")
    parser.add_argument("--key-id", default=None, help="explicit key id (default: generated)")
    parser.add_argument(
        "--merge",
        default=None,
        help="existing GUARDRAIL_API_KEYS_JSON to merge into, so current keys keep working",
    )
    args = parser.parse_args()

    raw, entry = mint(args.tenant, args.name, args.key_id)

    table: dict[str, object] = {}
    if args.merge:
        try:
            table = json.loads(args.merge)
        except json.JSONDecodeError as exc:
            print(f"error: --merge is not valid JSON: {exc}", file=sys.stderr)
            return 2
    table.update(entry)

    print("=" * 72)
    print("API KEY -- shown once, never stored. Copy it now.")
    print("=" * 72)
    print(f"\n  {raw}\n")
    print(f"tenant: {args.tenant}    name: {args.name}")
    print("\nPass this at deploy time (hashes only, safe to keep in your shell history):\n")
    print(f"  GUARDRAIL_API_KEYS_JSON='{json.dumps(table, separators=(',', ':'))}'\n")
    print("Use the key with:  -H 'x-api-key: <key>'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

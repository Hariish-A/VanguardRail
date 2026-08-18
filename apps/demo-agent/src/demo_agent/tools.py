"""The five tools the demo agent can reach, each governed.

Every one is wrapped by `@governed_tool`, so the policy check happens before the function
body. The bodies here are simulated -- they append to an in-process ledger rather than
touching a real database or mail server.

That simulation is load-bearing for the demo, not a shortcut. The `SIDE_EFFECTS` ledger is
what lets a test assert that a blocked call **did not execute**, which is the only way to
prove pre-execution enforcement rather than after-the-fact logging. Swapping a body for a
real `boto3` or SMTP call changes nothing about the guardrail path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from guardrail_sdk import governed_tool


@dataclass
class SideEffect:
    """A thing that actually happened."""

    tool: str
    detail: str
    arguments: dict[str, Any] = field(default_factory=dict)


SIDE_EFFECTS: list[SideEffect] = []
"""Append-only ledger of executed effects.

Empty after a blocked call. That assertion is the difference between a guardrail and a
log file.
"""


def reset_side_effects() -> None:
    SIDE_EFFECTS.clear()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@governed_tool("db.delete_records")
def db_delete_records(table: str, count: int = 0, where: str = "") -> str:
    """Delete rows from a table.

    Both `count` and `where` are accepted because agents supply either. A `where` clause
    yields an undeterminable row count, which the engine treats as UNKNOWN and fails
    closed on -- so an unbounded delete cannot slip past a threshold rule.
    """
    detail = (
        f"deleted {count} rows from {table}"
        if count
        else f"deleted rows from {table} where {where}"
    )
    SIDE_EFFECTS.append(
        SideEffect("db.delete_records", detail, {"table": table, "count": count, "where": where})
    )
    return detail


@governed_tool("email.send")
def email_send(to: str, subject: str, body: str = "", cc: str = "", bcc: str = "") -> str:
    """Send an email.

    `cc` and `bcc` are exposed deliberately: a policy that inspected only `to` would let
    data leave via `bcc`, and closing that gap is exactly the point of governing actions
    rather than text.
    """
    recipients = ", ".join(x for x in (to, cc, bcc) if x)
    detail = f"sent '{subject}' to {recipients}"
    SIDE_EFFECTS.append(
        SideEffect("email.send", detail, {"to": to, "subject": subject, "cc": cc, "bcc": bcc})
    )
    return detail


@governed_tool("file.read")
def file_read(path: str) -> str:
    """Read a file."""
    detail = f"read {path}"
    SIDE_EFFECTS.append(SideEffect("file.read", detail, {"path": path}))
    return f"<contents of {path}>"


@governed_tool("http.request")
def http_request(url: str, method: str = "GET", body: str = "") -> str:
    """Make an outbound HTTP request."""
    detail = f"{method} {url}"
    SIDE_EFFECTS.append(SideEffect("http.request", detail, {"url": url, "method": method}))
    return f"<response from {url}>"


@governed_tool("payments.refund")
def payments_refund(amount: float, customer_id: str, reason: str = "") -> str:
    """Refund a customer."""
    detail = f"refunded {amount} to {customer_id}"
    SIDE_EFFECTS.append(
        SideEffect("payments.refund", detail, {"amount": amount, "customer_id": customer_id})
    )
    return detail


# ---------------------------------------------------------------------------
# Wire format for the model
# ---------------------------------------------------------------------------

# OpenAI function names cannot contain dots, but policy tool names are dotted
# (`db.delete_records`). These two maps translate between the model's vocabulary and the
# policy's, so neither has to compromise for the other.
TOOL_REGISTRY = {
    "db_delete_records": db_delete_records,
    "email_send": email_send,
    "file_read": file_read,
    "http_request": http_request,
    "payments_refund": payments_refund,
}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "db_delete_records",
            "description": "Delete records from a database table. Destructive.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "description": "Table name."},
                    "count": {
                        "type": "integer",
                        "description": "How many records will be deleted, if known.",
                    },
                    "where": {
                        "type": "string",
                        "description": "SQL WHERE clause, if the count is not known.",
                    },
                },
                "required": ["table"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "email_send",
            "description": "Send an email to one or more recipients.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "Primary recipient address."},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                    "cc": {"type": "string"},
                    "bcc": {"type": "string"},
                },
                "required": ["to", "subject"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "file_read",
            "description": "Read the contents of a file from disk.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Absolute path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "Make an outbound HTTP request to an external service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE"]},
                    "body": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "payments_refund",
            "description": "Issue a refund to a customer.",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount": {"type": "number", "description": "Amount in USD."},
                    "customer_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["amount", "customer_id"],
            },
        },
    },
]

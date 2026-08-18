"""Preflight version parsing.

Regression cover for a real bug: `docker info` exited 0 while printing a connection
error, so the daemon check reported OK while Docker was down. A false OK on a blocking
dependency is worse than no check, because it defers the failure to deploy time.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from preflight import looks_like_version


@pytest.mark.parametrize("text", ["27.3.1", "24.0", "20.10.21", "27.3.1-rc2", " 27.3.1 "])
def test_real_versions_are_accepted(text: str) -> None:
    assert looks_like_version(text)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        'error during connect: Get "http://%2F%2F.%2Fpipe%2FdockerDesktopLinuxEngine/v1.47/info"',
        "Cannot connect to the Docker daemon",
        "The system cannot find the file specified.",
        "not found",
        "timed out",
    ],
)
def test_error_messages_are_rejected(text: str) -> None:
    """The exact strings a down daemon produces must never read as a version."""
    assert not looks_like_version(text)

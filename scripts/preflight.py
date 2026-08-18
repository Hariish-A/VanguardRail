"""Check that this machine is ready to deploy Guardrail, and say what to fix if not.

Run this before any deploy:

    uv run python scripts/preflight.py

Every check prints either what is working or the exact command to fix it. Nothing here
changes any state -- it only inspects.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

REQUIRED_REGION = "us-east-1"
"""CloudFront-associated resources must live in us-east-1. This is not a preference."""

Status = Literal["ok", "warn", "fail"]

_SYMBOL = {"ok": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]"}


@dataclass
class Result:
    """One check's outcome, plus how to fix it when it is not ok."""

    name: str
    status: Status
    detail: str
    fix: str = ""


def looks_like_version(text: str) -> bool:
    """Whether a string is a plausible version number rather than an error message.

    Exists because `docker info` can exit 0 while printing a connection error, so the
    exit code alone once produced a false OK on a down daemon -- deferring the failure
    all the way to deploy time.
    """
    return bool(re.fullmatch(r"\d+\.\d+[\w.\-+]*", text.strip()))


def _run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    """Run a command and return (exit code, combined output)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed command lists, no shell
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return proc.returncode, (proc.stdout + proc.stderr).strip()


# Default install locations, checked when a tool is absent from PATH. Windows applies
# PATH changes only to newly launched processes, so a tool installed minutes ago looks
# missing in an already-open terminal. Distinguishing "not installed" from "installed
# but this shell predates it" turns a dead-end FAIL into a one-line fix.
_FALLBACK_DIRS: dict[str, list[str]] = {
    "aws": [r"%LOCALAPPDATA%\AWSCLIV2\Amazon\AWSCLIV2"],
    "cdk": [r"%APPDATA%\npm"],
}


def find_tool(name: str) -> tuple[str | None, bool]:
    """Locate an executable.

    Returns (path, on_path). `on_path` is False when the tool exists in a known install
    directory but this process's PATH cannot see it.
    """
    if found := shutil.which(name):
        return found, True

    for raw in _FALLBACK_DIRS.get(name, []):
        directory = Path(os.path.expandvars(raw))
        if not directory.is_dir():
            continue
        for suffix in (".cmd", ".exe", ""):
            candidate = directory / f"{name}{suffix}"
            if candidate.is_file():
                return str(candidate), False

    return None, False


def _stale_path_fix(name: str) -> str:
    return (
        f"{name} IS installed, but this terminal's PATH predates the install. "
        "Close it and open a NEW terminal -- Windows applies PATH changes only to "
        "newly launched processes."
    )


def check_aws_cli() -> Result:
    """The AWS CLI is optional for CDK but makes verification far easier."""
    path, on_path = find_tool("aws")

    if path and not on_path:
        return Result(
            "AWS CLI", "warn", "installed, not on this shell's PATH", _stale_path_fix("aws")
        )
    if not path:
        return Result(
            "AWS CLI",
            "warn",
            "not found",
            "Optional. CDK reads ~/.aws directly. Install from "
            "https://awscli.amazonaws.com/AWSCLIV2.msi if you want `aws` commands.",
        )

    code, out = _run([path, "--version"])
    if code != 0:
        return Result("AWS CLI", "warn", out[:120], "Reinstall the AWS CLI.")
    return Result("AWS CLI", "ok", out.split()[0])


def check_cdk_cli() -> Result:
    """Required: the CDK CLI performs bootstrap and deploy."""
    path, on_path = find_tool("cdk")

    if path and not on_path:
        return Result(
            "CDK CLI", "warn", "installed, not on this shell's PATH", _stale_path_fix("cdk")
        )
    if not path:
        return Result("CDK CLI", "fail", "not found", "npm install --global aws-cdk@2")

    code, out = _run([path, "--version"])
    if code != 0:
        return Result("CDK CLI", "fail", out[:120], "npm install --global aws-cdk@2")
    return Result("CDK CLI", "ok", out.split()[0])


def check_credentials() -> Result:
    """Verify credentials actually work, rather than merely existing.

    A credentials file with a typo looks identical to a working one until deploy time,
    so this makes a real STS call.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
    except ImportError:
        return Result("AWS credentials", "fail", "boto3 missing", "uv sync --all-extras")

    try:
        identity = boto3.client("sts", region_name=REQUIRED_REGION).get_caller_identity()
    except NoCredentialsError:
        return Result(
            "AWS credentials",
            "fail",
            "no credentials found",
            "Run: aws configure   (see PROGRESS.md section 7 for how to get an access key)",
        )
    except (ClientError, BotoCoreError) as exc:
        return Result(
            "AWS credentials",
            "fail",
            f"credentials rejected: {type(exc).__name__}",
            "The access key is wrong, disabled, or deleted. Create a new one in IAM.",
        )

    arn = identity["Arn"]
    account = identity["Account"]

    if ":root" in arn:
        return Result(
            "AWS credentials",
            "warn",
            f"account {account}, using ROOT credentials",
            "Root access keys should not be used. Create an IAM user with "
            "AdministratorAccess, generate a key for it, and re-run `aws configure`.",
        )

    return Result("AWS credentials", "ok", f"account {account}, {arn.split('/')[-1]}")


def check_region() -> Result:
    """CloudFront-associated resources must be created in us-east-1."""
    try:
        import boto3
    except ImportError:
        return Result("Region", "fail", "boto3 missing", "uv sync --all-extras")

    region = boto3.session.Session().region_name

    if region is None:
        return Result(
            "Region",
            "fail",
            "no default region configured",
            f"aws configure set region {REQUIRED_REGION}",
        )
    if region != REQUIRED_REGION:
        return Result(
            "Region",
            "fail",
            f"configured as {region}",
            f"Must be {REQUIRED_REGION} -- CloudFront requires it. "
            f"Run: aws configure set region {REQUIRED_REGION}",
        )
    return Result("Region", "ok", region)


def check_docker() -> Result:
    """Docker is required to bundle the Lambda zip, but not to synth or test."""
    if not shutil.which("docker"):
        return Result(
            "Docker",
            "fail",
            "not installed",
            "Install Docker Desktop. Required to bundle the Lambda deployment package.",
        )
    code, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=45)

    # `docker info` can exit 0 while printing a connection error, so the exit code
    # alone is not trustworthy. Require output that actually looks like a version --
    # a false OK here is worse than no check, because it defers the failure to deploy.
    version = out.splitlines()[-1].strip() if out else ""
    daemon_up = code == 0 and looks_like_version(version)

    if not daemon_up:
        return Result(
            "Docker",
            "fail",
            "installed, but the daemon is not responding",
            "Start Docker Desktop and wait until it reports 'Engine running'. "
            "Needed only to bundle the Lambda zip -- tests and synth do not require it.",
        )
    return Result("Docker", "ok", f"daemon {version}")


def check_bootstrap() -> Result:
    """CDK needs a one-time bootstrap per account+region before the first deploy."""
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError
    except ImportError:
        return Result("CDK bootstrap", "fail", "boto3 missing", "uv sync --all-extras")

    try:
        cfn = boto3.client("cloudformation", region_name=REQUIRED_REGION)
        stack = cfn.describe_stacks(StackName="CDKToolkit")["Stacks"][0]
    except (ClientError, BotoCoreError):
        return Result(
            "CDK bootstrap",
            "fail",
            "CDKToolkit stack not found",
            "cd infra && cdk bootstrap aws://<ACCOUNT_ID>/us-east-1",
        )

    if not stack["StackStatus"].endswith(("_COMPLETE",)):
        return Result(
            "CDK bootstrap",
            "warn",
            f"CDKToolkit is {stack['StackStatus']}",
            "Wait for it to finish, or re-run cdk bootstrap.",
        )
    return Result("CDK bootstrap", "ok", stack["StackStatus"])


def check_alert_email() -> Result:
    """Without this the budget stack is skipped, leaving no cost tripwire."""
    email = os.environ.get("GUARDRAIL_ALERT_EMAIL", "").strip()
    if not email:
        return Result(
            "Cost alarm email",
            "warn",
            "GUARDRAIL_ALERT_EMAIL not set",
            "The zero-spend budget alarm will be SKIPPED. Set it in .env "
            "(copy .env.example) so overspend is noticed within hours.",
        )
    if "@" not in email:
        return Result("Cost alarm email", "fail", f"{email!r} is not an address", "Fix .env")
    return Result("Cost alarm email", "ok", email)


CHECKS = [
    check_aws_cli,
    check_cdk_cli,
    check_credentials,
    check_region,
    check_docker,
    check_bootstrap,
    check_alert_email,
]


def main() -> int:
    # Load .env if present, so GUARDRAIL_ALERT_EMAIL is seen the same way the app sees it.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    print("Guardrail deployment preflight\n" + "=" * 60)

    results = [check() for check in CHECKS]

    for result in results:
        print(f"{_SYMBOL[result.status]}  {result.name:<20} {result.detail}")

    problems = [r for r in results if r.status != "ok"]

    if problems:
        print("\n" + "=" * 60)
        print("To fix:\n")
        for result in problems:
            print(f"  {result.name} ({result.status})")
            print(f"    {result.fix}\n")

    failures = [r for r in results if r.status == "fail"]
    if failures:
        print(f"NOT READY: {len(failures)} blocking issue(s).")
        return 1

    warnings = [r for r in results if r.status == "warn"]
    if warnings:
        print(f"READY, with {len(warnings)} warning(s) above.")
        return 0

    print("READY to deploy: cd infra && cdk deploy --all")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

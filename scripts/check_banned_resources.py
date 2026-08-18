"""Fail the build if the synthesized template contains anything that costs money.

The plan commits to a $0 AWS bill. Commitments that live only in a document decay: six
milestones from now, someone adds a NAT gateway for a perfectly sensible reason and the
constraint is quietly gone. This script turns the commitment into a gate.

It reads `cdk synth` output rather than the CDK source, so it catches resources added
indirectly by an L2 construct's defaults -- which is exactly how a NAT gateway or an
on-demand table usually appears.

Usage:
    cd infra && cdk synth --quiet
    python ../scripts/check_banned_resources.py infra/cdk.out
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# CloudFormation types that carry a recurring charge with no free-tier escape.
BANNED_TYPES: dict[str, str] = {
    "AWS::EC2::NatGateway": "~$32/month, and there is no free tier",
    "AWS::EC2::Instance": "EC2 has no always-free tier since the 2025 free-plan change",
    "AWS::ElasticLoadBalancingV2::LoadBalancer": "~$16/month minimum",
    "AWS::ElasticLoadBalancing::LoadBalancer": "~$16/month minimum",
    "AWS::ECS::Cluster": "Fargate/EC2 tasks bill continuously",
    "AWS::ECS::Service": "Fargate/EC2 tasks bill continuously",
    "AWS::EKS::Cluster": "$0.10/hour for the control plane alone",
    "AWS::WAFv2::WebACL": "$5/month per web ACL",
    "AWS::SecretsManager::Secret": "$0.40/secret/month -- use SSM Parameter Store instead",
    "AWS::ECR::Repository": "500 MB free for 12 months only; we ship zip Lambdas, not images",
    "AWS::RDS::DBInstance": "no always-free tier",
    "AWS::Elasticsearch::Domain": "no always-free tier",
    "AWS::OpenSearchService::Domain": "no always-free tier",
    "AWS::Kinesis::Stream": "shard-hours bill continuously",
    "AWS::MSK::Cluster": "bills continuously",
    "AWS::EFS::FileSystem": "no always-free tier",
    "AWS::DocDB::DBCluster": "no always-free tier",
    "AWS::EC2::VPCEndpoint": "interface endpoints bill per hour",
}


def _iter_templates(cdk_out: Path) -> Iterator[Path]:
    """Yield every synthesized CloudFormation template."""
    yield from sorted(cdk_out.glob("*.template.json"))


def _check_dynamodb_billing(
    logical_id: str,
    resource: dict[str, Any],
) -> str | None:
    """Reject on-demand DynamoDB tables.

    The DynamoDB free tier covers 25 GB of storage plus 25 WCU/25 RCU of *provisioned*
    capacity. On-demand (PAY_PER_REQUEST) has no free tier at all and bills from the
    very first request -- an easy and expensive mistake, because on-demand is the more
    natural default everywhere else.
    """
    properties = resource.get("Properties", {})
    billing_mode = properties.get("BillingMode")

    if billing_mode == "PAY_PER_REQUEST":
        return (
            f"{logical_id}: DynamoDB table uses PAY_PER_REQUEST. The free tier covers "
            "provisioned capacity only (25 WCU / 25 RCU). Use BillingMode.PROVISIONED."
        )

    # Absent BillingMode means provisioned, which then requires explicit throughput.
    if billing_mode is None and "ProvisionedThroughput" not in properties:
        return (
            f"{logical_id}: DynamoDB table declares neither BillingMode nor ProvisionedThroughput"
        )

    return None


def _check_log_retention(logical_id: str, resource: dict[str, Any]) -> str | None:
    """Reject log groups that never expire.

    Lambda's default is infinite retention, which is the most common way to drift past
    the 5 GB CloudWatch Logs allowance and start paying.
    """
    if "RetentionInDays" not in resource.get("Properties", {}):
        return (
            f"{logical_id}: log group has no RetentionInDays, so it retains forever "
            "and will eventually exceed the 5 GB free allowance"
        )
    return None


def check_template(path: Path) -> list[str]:
    """Return every cost violation found in one synthesized template."""
    template = json.loads(path.read_text(encoding="utf-8"))
    resources: dict[str, Any] = template.get("Resources", {})
    violations: list[str] = []

    for logical_id, resource in resources.items():
        resource_type = resource.get("Type", "")

        if reason := BANNED_TYPES.get(resource_type):
            violations.append(f"{logical_id} ({resource_type}): {reason}")

        if resource_type == "AWS::DynamoDB::Table" and (
            issue := _check_dynamodb_billing(logical_id, resource)
        ):
            violations.append(issue)

        if resource_type == "AWS::Logs::LogGroup" and (
            issue := _check_log_retention(logical_id, resource)
        ):
            violations.append(issue)

    return violations


def main(argv: list[str]) -> int:
    cdk_out = Path(argv[1] if len(argv) > 1 else "infra/cdk.out")

    if not cdk_out.is_dir():
        print(f"error: {cdk_out} does not exist -- run `cdk synth` first", file=sys.stderr)
        return 2

    templates = list(_iter_templates(cdk_out))
    if not templates:
        print(f"error: no *.template.json found in {cdk_out}", file=sys.stderr)
        return 2

    total = 0
    for template in templates:
        violations = check_template(template)
        if violations:
            total += len(violations)
            print(f"\n{template.name}:", file=sys.stderr)
            for violation in violations:
                print(f"  ! {violation}", file=sys.stderr)

    if total:
        print(
            f"\nFAIL: {total} cost violation(s). This project runs on the AWS always-free "
            "tier; see the budget table in the plan before adding a paid resource.",
            file=sys.stderr,
        )
        return 1

    names = ", ".join(t.name for t in templates)
    print(f"OK: {len(templates)} template(s) contain no paid resources ({names})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

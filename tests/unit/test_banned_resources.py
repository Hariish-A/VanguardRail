"""The cost gate must actually fail on the mistakes it exists to prevent.

An untested guard is worse than no guard: it produces confidence without protection.
Each test below encodes one specific way this project could start costing money.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from check_banned_resources import check_template, main


def _write_template(tmp_path: Path, resources: dict[str, Any], name: str = "Test") -> Path:
    path = tmp_path / f"{name}.template.json"
    path.write_text(json.dumps({"Resources": resources}), encoding="utf-8")
    return path


# A minimal always-free stack, used as the "should pass" baseline.
CLEAN_RESOURCES: dict[str, Any] = {
    "Logs": {
        "Type": "AWS::Logs::LogGroup",
        "Properties": {"RetentionInDays": 7},
    },
    "Fn": {"Type": "AWS::Lambda::Function", "Properties": {"MemorySize": 512}},
    "Table": {
        "Type": "AWS::DynamoDB::Table",
        "Properties": {
            "BillingMode": "PROVISIONED",
            "ProvisionedThroughput": {"ReadCapacityUnits": 5, "WriteCapacityUnits": 5},
        },
    },
}


def test_clean_template_passes(tmp_path: Path) -> None:
    path = _write_template(tmp_path, CLEAN_RESOURCES)

    assert check_template(path) == []


@pytest.mark.parametrize(
    "resource_type",
    [
        "AWS::EC2::NatGateway",
        "AWS::ElasticLoadBalancingV2::LoadBalancer",
        "AWS::ECS::Service",
        "AWS::EKS::Cluster",
        "AWS::WAFv2::WebACL",
        "AWS::SecretsManager::Secret",
        "AWS::ECR::Repository",
        "AWS::RDS::DBInstance",
    ],
)
def test_each_banned_resource_is_rejected(tmp_path: Path, resource_type: str) -> None:
    """Every entry in the ban list must be caught, not just the famous ones."""
    path = _write_template(tmp_path, {"Bad": {"Type": resource_type, "Properties": {}}})

    violations = check_template(path)

    assert len(violations) == 1
    assert resource_type in violations[0]


def test_on_demand_dynamodb_is_rejected(tmp_path: Path) -> None:
    """The DynamoDB free tier covers provisioned capacity only.

    On-demand bills from the first request, and it is the more natural default
    everywhere else, which makes this the single easiest way to start paying.
    """
    path = _write_template(
        tmp_path,
        {
            "Table": {
                "Type": "AWS::DynamoDB::Table",
                "Properties": {"BillingMode": "PAY_PER_REQUEST"},
            }
        },
    )

    violations = check_template(path)

    assert len(violations) == 1
    assert "PAY_PER_REQUEST" in violations[0]


def test_dynamodb_without_any_capacity_declaration_is_rejected(tmp_path: Path) -> None:
    path = _write_template(tmp_path, {"Table": {"Type": "AWS::DynamoDB::Table", "Properties": {}}})

    assert len(check_template(path)) == 1


def test_log_group_without_retention_is_rejected(tmp_path: Path) -> None:
    """Lambda's default is infinite retention, which walks past the 5 GB allowance."""
    path = _write_template(tmp_path, {"Logs": {"Type": "AWS::Logs::LogGroup", "Properties": {}}})

    violations = check_template(path)

    assert len(violations) == 1
    assert "RetentionInDays" in violations[0]


def test_multiple_violations_are_all_reported(tmp_path: Path) -> None:
    """Reporting only the first would mean N runs to find N problems."""
    path = _write_template(
        tmp_path,
        {
            "Nat": {"Type": "AWS::EC2::NatGateway", "Properties": {}},
            "Alb": {"Type": "AWS::ElasticLoadBalancingV2::LoadBalancer", "Properties": {}},
            "Logs": {"Type": "AWS::Logs::LogGroup", "Properties": {}},
        },
    )

    assert len(check_template(path)) == 3


def test_main_returns_nonzero_on_violation(tmp_path: Path) -> None:
    _write_template(tmp_path, {"Nat": {"Type": "AWS::EC2::NatGateway", "Properties": {}}})

    assert main(["check", str(tmp_path)]) == 1


def test_main_returns_zero_on_clean_output(tmp_path: Path) -> None:
    _write_template(tmp_path, CLEAN_RESOURCES)

    assert main(["check", str(tmp_path)]) == 0


def test_missing_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """A typo in the CI path must fail the build rather than silently succeed."""
    assert main(["check", str(tmp_path / "nope")]) == 2


def test_empty_directory_is_an_error_not_a_pass(tmp_path: Path) -> None:
    """Likewise if `cdk synth` produced nothing -- passing here would be a false green."""
    assert main(["check", str(tmp_path)]) == 2

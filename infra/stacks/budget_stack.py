"""The cost tripwire.

The plan commits to a $0 build. That commitment is worth nothing unless a breach is
noticed within hours rather than at the end of the month, so this stack exists purely
to make overspend loud.

It is deployed once (it is account-wide, not per-stage) and deliberately kept separate
from the service stack: a `cdk destroy` of the service must never take the alarm with
it.

AWS Budgets allows two budgets free of charge, which is what this costs: one. The
earlier wording here said "the first two SNS email notifications", conflating the free
budget allowance with SNS delivery -- they are separate allowances and the sentence was
misleading about both. Budget notifications are delivered by AWS to the address below;
the alarm notifications for the service stack use a separate SNS topic.
"""

from __future__ import annotations

from typing import Any

from aws_cdk import Stack
from aws_cdk import aws_budgets as budgets
from constructs import Construct

# Deliberately near-zero. This is not a spending limit -- it is a smoke detector. Any
# non-zero charge on this account means something outside the always-free tier was
# provisioned, and that is worth an email the same day.
THRESHOLD_USD = 1.0


class BudgetStack(Stack):
    """An AWS Budget that emails the moment real spend appears."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        notification_email: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        subscribers = [
            budgets.CfnBudget.SubscriberProperty(
                address=notification_email,
                subscription_type="EMAIL",
            )
        ]

        budgets.CfnBudget(
            self,
            "ZeroSpendBudget",
            budget=budgets.CfnBudget.BudgetDataProperty(
                budget_name="guardrail-zero-spend",
                budget_type="COST",
                time_unit="MONTHLY",
                budget_limit=budgets.CfnBudget.SpendProperty(
                    amount=THRESHOLD_USD,
                    unit="USD",
                ),
            ),
            notifications_with_subscribers=[
                # Fires on money already spent.
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="ACTUAL",
                        comparison_operator="GREATER_THAN",
                        threshold=1.0,  # percent of the limit, i.e. one cent
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                ),
                # Fires on AWS's own projection, which usually lands days earlier and is
                # the notification that actually saves money.
                budgets.CfnBudget.NotificationWithSubscribersProperty(
                    notification=budgets.CfnBudget.NotificationProperty(
                        notification_type="FORECASTED",
                        comparison_operator="GREATER_THAN",
                        threshold=50.0,
                        threshold_type="PERCENTAGE",
                    ),
                    subscribers=subscribers,
                ),
            ],
        )

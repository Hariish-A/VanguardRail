"""AWS Lambda entrypoint.

This is the entire difference between the Lambda deployment and the container
deployment: Mangum translates the Lambda Function URL event into ASGI. Everything
else — routing, validation, policy evaluation — is shared.
"""

from __future__ import annotations

from typing import Any

from mangum import Mangum

from guardrail_service.app import app

_mangum = Mangum(app, lifespan="off")


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Adapt a Lambda Function URL invocation to the ASGI app.

    Lambda Function URLs deliver API Gateway v2 payloads, which Mangum handles
    natively. `lifespan` is off because Lambda freezes the container between
    invocations, so ASGI startup/shutdown events would fire at times that do not
    correspond to anything meaningful.
    """
    result: dict[str, Any] = _mangum(event, context)
    return result

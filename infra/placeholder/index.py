"""Placeholder asset used only when GUARDRAIL_SKIP_BUNDLING is set.

Present so `cdk synth` can run without Docker, for template validation and the
banned-resource cost scan. A real deploy always bundles the actual packages.
"""


def lambda_handler(event: dict, context: object) -> dict:
    raise RuntimeError(
        "This is a placeholder build produced with GUARDRAIL_SKIP_BUNDLING set. "
        "Deploy with Docker running so the real packages are bundled."
    )

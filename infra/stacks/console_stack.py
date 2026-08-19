"""The review console, hosted on S3.

A separate stack, for the same reason the agent is one: a failure building or deploying
a frontend must never put the control plane at risk, and `cdk destroy` on the console
cannot touch the audit table.

## Why S3 static hosting and not CloudFront

CloudFront in front of a private bucket is the textbook answer, and it is the one this
project would use if it could. **A new AWS account cannot create a distribution without a
support case**, which is the same constraint that removed CloudFront from the service
stack. So the bucket serves the objects itself.

Two endpoints come out of that, and they are not equivalent:

* the **REST endpoint** (`https://<bucket>.s3.<region>.amazonaws.com/index.html`) is
  HTTPS, and is the one to hand to anybody;
* the **website endpoint** (`http://<bucket>.s3-website-<region>.amazonaws.com`) is
  prettier and supports an index document, but is **HTTP only** — S3 website hosting
  cannot terminate TLS.

The console is a single `index.html` plus hashed assets and routes with `#/`, so the REST
endpoint works completely: a hash never reaches the server, which means deep links and
refreshes both resolve without any rewrite rule. HTTPS therefore costs nothing here, and
the website endpoint is emitted only as a convenience.

An HTTP page calling an HTTPS API is allowed by browsers — mixed-content blocking runs the
other way round. It is still worse: the page itself is served unauthenticated over plain
HTTP and could be tampered with in transit, and this page takes an API key. **Use the
HTTPS URL.** That is stated in the outputs rather than left for someone to work out.

## Cost

S3 is the one Tier B service in this project: 5 GB for 12 months, then about $0.023/GB.
The built bundle is roughly half a megabyte, so the honest figure after the free window
expires is a fraction of a cent per month. Every other service here is always-free. No
CloudFront, no ACM certificate, no Route 53 zone.
"""

from __future__ import annotations

import os
from pathlib import Path

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

CONSOLE_DIST = Path(__file__).resolve().parents[2] / "apps" / "console-ui" / "dist"

SKIP_BUNDLING = os.environ.get("GUARDRAIL_SKIP_BUNDLING", "").lower() in {"1", "true", "yes"}
"""Set by the offline cost gate, which synthesizes without Docker and without a build.

The gate's job is to prove no billable resource is declared; it does not need the actual
bundle. A placeholder asset keeps `cdk synth` working on a clean checkout.
"""


class ConsoleStack(Stack):
    """Static hosting for the React review console."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        stage: str,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)  # type: ignore[arg-type]

        self.stage = stage

        bucket = self._create_bucket()
        self._deploy_site(bucket)
        self._create_outputs(bucket)

    # ------------------------------------------------------------------
    def _create_bucket(self) -> s3.Bucket:
        """A public-read bucket, named deterministically.

        The name is derived from the account and stage rather than left to CDK, because
        the service stack's CORS allowlist has to name this origin — and an allowlist
        that can only be written *after* the bucket exists would make the first deploy of
        a fresh account a two-pass affair.

        `block_public_policy` and `restrict_public_buckets` are turned off deliberately
        and only here. This bucket holds a compiled frontend: every byte in it is meant to
        be world-readable, and there is nothing else in it. `block_public_acls` stays on,
        so the *only* way anything becomes public is the bucket policy declared below —
        object-level ACLs cannot widen it by accident.
        """
        return s3.Bucket(
            self,
            "ConsoleBucket",
            bucket_name=f"guardrail-console-{self.stage}-{self.account}",
            website_index_document="index.html",
            # Not a real 404 page: with hash routing every URL resolves to index.html, and
            # returning it for an unknown path keeps a mistyped link working.
            website_error_document="index.html",
            public_read_access=True,
            block_public_access=s3.BlockPublicAccess(
                block_public_acls=True,
                ignore_public_acls=True,
                block_public_policy=False,
                restrict_public_buckets=False,
            ),
            # The bucket is a build artifact. Nothing in it is a source of truth, so it is
            # safe to remove entirely with the stack — unlike the audit table.
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=False,
            lifecycle_rules=[
                # Old asset hashes accumulate on every deploy; without this the bucket
                # would creep toward the 5 GB free allowance one build at a time.
                s3.LifecycleRule(
                    id="expire-noncurrent-builds",
                    noncurrent_version_expiration=Duration.days(7),
                    abort_incomplete_multipart_upload_after=Duration.days(1),
                )
            ],
        )

    def _deploy_site(self, bucket: s3.Bucket) -> None:
        """Upload the built console.

        Fails the synth with a readable message rather than deploying an empty bucket. A
        console that returns 404 looks like an outage of the *service*, and someone would
        reasonably start debugging the Lambda.
        """
        if SKIP_BUNDLING:
            return

        if not (CONSOLE_DIST / "index.html").exists():
            raise ValueError(
                f"The console has not been built: {CONSOLE_DIST / 'index.html'} is "
                "missing. Run `npm ci && npm run build` in apps/console-ui first. "
                "Deploying without it would publish an empty bucket, and a console "
                "returning 404 reads as a service outage rather than a missing build."
            )

        s3deploy.BucketDeployment(
            self,
            "ConsoleDeployment",
            sources=[s3deploy.Source.asset(str(CONSOLE_DIST))],
            destination_bucket=bucket,
            # Removes files from a previous build that this one no longer produces.
            prune=True,
            # Vite fingerprints asset filenames, so they are safe to cache hard.
            # index.html is not fingerprinted and must never be cached, or a deploy would
            # keep serving the old bundle from a browser that has already visited.
            cache_control=[
                s3deploy.CacheControl.from_string("public, max-age=31536000, immutable")
            ],
            exclude=["index.html"],
        )

        s3deploy.BucketDeployment(
            self,
            "ConsoleIndexDeployment",
            sources=[s3deploy.Source.asset(str(CONSOLE_DIST), exclude=["assets/*"])],
            destination_bucket=bucket,
            prune=False,
            cache_control=[s3deploy.CacheControl.from_string("no-cache, must-revalidate")],
        )

    def _create_outputs(self, bucket: s3.Bucket) -> None:
        https_url = f"https://{bucket.bucket_name}.s3.{self.region}.amazonaws.com/index.html"

        CfnOutput(
            self,
            "ConsoleUrl",
            value=https_url,
            description="The console. HTTPS — hand out this one.",
        )
        CfnOutput(
            self,
            "ConsoleWebsiteUrl",
            value=bucket.bucket_website_url,
            description=(
                "S3 website endpoint. Shorter, but HTTP only — S3 website hosting cannot "
                "terminate TLS. Prefer ConsoleUrl, which is HTTPS."
            ),
        )
        CfnOutput(
            self,
            "ConsoleBucketName",
            value=bucket.bucket_name,
            description=(
                "Add this origin to GUARDRAIL_CONSOLE_ORIGINS before deploying the service."
            ),
        )
        CfnOutput(
            self,
            "ConsoleAllowedOrigin",
            value=f"https://{bucket.bucket_name}.s3.{self.region}.amazonaws.com",
            description=(
                "The exact Origin header this page sends. The service stack's CORS "
                "allowlist must contain it verbatim -- scheme included."
            ),
        )

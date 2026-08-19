# Guardrail control plane, as a portable container.
#
# This image is NOT how the service reaches AWS. Lambda is deployed as a zip plus a
# dependency layer, because container-image Lambdas require a private ECR repository
# (500 MB free for 12 months, then $0.10/GB-month) and this dependency set fits well
# inside the 250 MB zip limit anyway.
#
# The image earns its place three other ways:
#   1. local development parity, via docker-compose
#   2. the CI test runner, so "works on my machine" stops being a class of bug
#   3. proof of portability -- the same FastAPI app runs on ECS, EKS, Cloud Run, or
#      on-prem Kubernetes with no code change, which is what keeps the control plane
#      from being Lambda-locked
#
# The only difference between this and the Lambda deployment is the entrypoint:
# uvicorn here, Mangum there.

FROM python:3.12-slim AS builder

# uv resolves and installs far faster than pip, which matters in CI.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /build

# Copy only what is needed to resolve dependencies first, so the layer cache survives
# source edits.
COPY pyproject.toml uv.lock ./
COPY packages/guardrail-core/pyproject.toml packages/guardrail-core/
COPY packages/guardrail-service/pyproject.toml packages/guardrail-service/
COPY infra/pyproject.toml infra/

COPY packages/ packages/

# `--no-editable` is load-bearing, not tidiness. Without it uv honours the workspace
# sources in guardrail-service's pyproject and installs guardrail-core as an editable:
# site-packages gets a .pth file pointing at /build/packages/guardrail-core/src, which
# exists only in this stage. Copying site-packages into the runtime stage then carries
# a dangling pointer and the container dies at import with
# "ModuleNotFoundError: No module named guardrail_core".
#
# The image had never been built and run until M5, so this shipped broken from M0
# while the Dockerfile claimed to prove portability.
RUN uv pip install --system --no-cache --no-editable \
    ./packages/guardrail-core \
    ./packages/guardrail-service \
    "uvicorn[standard]>=0.30"


FROM python:3.12-slim AS runtime

# Run as a non-root user. A guardrail service is a security control; giving it root in
# its own container undercuts the point.
RUN groupadd --system guardrail \
    && useradd --system --gid guardrail --create-home guardrail

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin/uvicorn /usr/local/bin/uvicorn

USER guardrail
WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    GUARDRAIL_STAGE=local \
    GUARDRAIL_LOG_LEVEL=INFO

EXPOSE 8080

# Hits the real liveness endpoint rather than just checking the port is open, so a
# process that is up but broken is reported as unhealthy.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "guardrail_service.app:app", "--host", "0.0.0.0", "--port", "8080"]

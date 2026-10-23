# Dockerfile for local test environment parity (OPTIONAL).
# Never includes credentials, tokens, or production data.
# Thin-wrapper constraint: no dependency installation occurs here.
# The orchestrator uses only the Python standard library; any HTTP client
# libraries needed to reach external engine endpoints must be pre-provided
# by the environment, never installed by this file. requirements.txt, if
# present, contains only thin-wrapper HTTP client libraries and is a
# documentation artifact only -- it is NOT installed at build time.

# Base image reference is configurable so that reproducible "test
# environment parity" builds can pin an immutable sha256 digest without
# editing this file. The default below is the mutable :3.12 tag for local
# convenience only; parity/CI builds MUST override it with a digest-pinned
# reference recorded at build time, e.g.:
#   docker build --build-arg BASE_IMAGE=public.ecr.aws/lambda/python@sha256:<digest> .
# The literal digest is environment data and is intentionally not hardcoded
# in this repository.
ARG BASE_IMAGE=public.ecr.aws/lambda/python:3.12
FROM ${BASE_IMAGE}

# Copy function code (src/ must be an importable package, e.g. contain
# __init__.py, so the dotted handler path below resolves).
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# NOTE: intentionally no `RUN pip install`. Installing dependencies at
# build time would violate the thin-wrapper "no dependency installation"
# rule and would execute arbitrary setup code (supply-chain risk). Because
# no pip install step exists in this image, a --no-cache-dir flag has
# nothing to apply to and is deliberately omitted; no pip cache layer is
# ever created, so image layers stay minimal by construction.

# Set the CMD to your handler (dotted module path, as required by the
# AWS Lambda Python runtime / RIE).
CMD ["src.orchestrator.handler"]

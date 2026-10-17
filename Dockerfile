FROM python:3.12-slim

WORKDIR /app
# Runtime dependencies, matching pyproject.toml. The image copies source instead
# of installing the project, so nothing else pulls these in, and a missing one
# only surfaces as an ImportError when a module that needs it is imported.
# cryptography signs the GitHub App JWT; there is no stdlib RSA, and shelling
# out to openssl would put the signing key behind a subprocess.
RUN pip install --no-cache-dir "PyYAML>=6.0,<7" "cryptography>=42,<47"
COPY src /app/src
COPY config /app/config
COPY examples /app/examples

ENV PYTHONPATH=/app/src
# stdout is block-buffered when not a TTY, and serve() never exits, so without
# this the fail-closed startup warnings sit in the buffer forever and the
# operator never sees that a webhook secret or allowlist is missing
ENV PYTHONUNBUFFERED=1
ENV CINDRAL_POLICY=/app/config/policy.toml
ENV CINDRAL_STATE=/app/examples/state.json

# Build metadata the broker reports on /healthz and /metrics. These are build
# args, not a file copied from the source: a source checkout has no build to
# describe, and a file in the image would report whatever it said rather than
# what was actually built.
ARG CINDRAL_VERSION=unknown
ARG CINDRAL_COMMIT=unknown
ARG CINDRAL_BUILT_AT=unknown
ENV CINDRAL_VERSION=$CINDRAL_VERSION
ENV CINDRAL_COMMIT=$CINDRAL_COMMIT
ENV CINDRAL_BUILT_AT=$CINDRAL_BUILT_AT

USER 65532:65532
EXPOSE 8095
ENTRYPOINT ["python", "-m", "cindral.cli"]
CMD ["serve", "--policy", "/app/config/policy.toml", "--state", "/app/examples/state.json", "--host", "0.0.0.0", "--port", "8095"]

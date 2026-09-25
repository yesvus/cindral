FROM python:3.12-slim

WORKDIR /app
# Runtime dependency, matching pyproject.toml. The image copies source instead
# of installing the project, so nothing else pulls this in, and a missing yaml
# only surfaces as an ImportError when a module that needs it is imported.
RUN pip install --no-cache-dir "PyYAML>=6.0,<7"
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

USER 65532:65532
EXPOSE 8095
ENTRYPOINT ["python", "-m", "cindral.cli"]
CMD ["serve", "--policy", "/app/config/policy.toml", "--state", "/app/examples/state.json", "--host", "0.0.0.0", "--port", "8095"]

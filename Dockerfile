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
ENV RUNNER_RELAY_POLICY=/app/config/policy.toml
ENV RUNNER_RELAY_STATE=/app/examples/state.json

USER 65532:65532
EXPOSE 8095
ENTRYPOINT ["python", "-m", "runner_relay.cli"]
CMD ["serve", "--policy", "/app/config/policy.toml", "--state", "/app/examples/state.json", "--host", "0.0.0.0", "--port", "8095"]

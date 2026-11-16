# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c

RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 gh=2.23.0+dfsg1-1 ca-certificates=20230311+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/runner runner \
    && install -d -o runner -g runner -m 0700 /var/lib/coding-runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    NQ_RUNNER_STATE_DIR=/var/lib/coding-runner
WORKDIR /opt/runner
COPY coding_runner /opt/runner/coding_runner
USER 10001:10001
ENTRYPOINT ["python", "-m", "coding_runner"]

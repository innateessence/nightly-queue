# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends git gh ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/runner runner \
    && install -d -o runner -g runner -m 0700 /var/lib/vibearr
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 VIBEARR_IMAGE=runner
WORKDIR /opt/runner
COPY coding_runner /opt/runner/coding_runner
COPY vibearr_runner /opt/runner/vibearr_runner
COPY vibearr_executor /opt/runner/vibearr_executor
USER 10001:10001
ENTRYPOINT ["python", "-m", "vibearr_runner"]

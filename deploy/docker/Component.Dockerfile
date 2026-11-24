# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home component \
    && mkdir /output && chown 10001:10001 /output
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY git /git-input
COPY component_ci.py /component_ci.py
COPY component-ci.sh /component-ci.sh
COPY Component.Dockerfile /Component.Dockerfile
COPY component-prerequisites.json /component-prerequisites.json
WORKDIR /tmp
USER 10001:10001
ENTRYPOINT ["python", "-I", "/component_ci.py"]

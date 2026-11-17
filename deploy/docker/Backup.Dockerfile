# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS python
FROM postgres:17@sha256:31ba9c94a03ada9988f2bb493a22a75eef3a185884533e5cdbcd498ea835b08f
COPY --from=python /usr/local /usr/local
RUN ldconfig && python3.12 -I -c 'import hashlib, json, ssl, tarfile, tempfile'
COPY deploy /usr/local/lib/python3.12/site-packages/deploy
WORKDIR /tmp
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
ENTRYPOINT ["python3.12", "-I", "-m", "deploy.docker.backup"]

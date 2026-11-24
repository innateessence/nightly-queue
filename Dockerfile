# syntax=docker/dockerfile:1
FROM rhysd/actionlint:1.7.7@sha256:887a259a5a534f3c4f36cb02dca341673c6089431057242cdc931e9f133147e9 AS workflow-lint
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp XDG_CACHE_HOME=/tmp/cache RUFF_CACHE_DIR=/tmp/cache/ruff MYPY_CACHE_DIR=/tmp/cache/mypy
WORKDIR /app
RUN useradd --uid 10001 --create-home appuser
COPY requirements.txt /opt/vibearr/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/vibearr/requirements.txt
RUN python -m pip install --no-cache-dir setuptools==80.9.0 wheel==0.48.0
ENTRYPOINT ["python", "-m", "deploy.docker.entrypoint"]

FROM base AS tools-base
COPY --from=workflow-lint /usr/local/bin/actionlint /usr/local/bin/actionlint
RUN apt-get update && apt-get install -y --no-install-recommends git postgresql-client ca-certificates shellcheck \
    && rm -rf /var/lib/apt/lists/*

FROM tools-base AS tools
COPY . /app
RUN --network=none python -m pip install --no-cache-dir --no-build-isolation --no-deps .
USER 10001:10001
CMD ["python", "manage.py", "check"]

FROM base AS application
COPY . /app
RUN --network=none python -m pip install --no-cache-dir --no-build-isolation --no-deps .
RUN --network=none python manage.py collectstatic --noinput --clear
USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "deploy.docker.application"]

FROM base AS runner-binaries
COPY deploy/docker/install_agents.py /opt/install_agents.py
RUN python /opt/install_agents.py
RUN codex --version && claude --version && opencode --version

FROM tools-base AS worker-base
LABEL io.vibearr.role="worker-base"
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-15 iptables procps \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home task \
    && useradd --uid 10002 --no-create-home testdatabase \
    && mkdir -p /work /home/task /input /secrets /control /test-database /opt/candidate /opt/validation /opt/executor
COPY --from=runner-binaries /usr/local/bin/codex /usr/local/bin/codex
COPY --from=runner-binaries /usr/local/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host
COPY --from=runner-binaries /usr/local/bin/claude /usr/local/bin/claude
COPY --from=runner-binaries /usr/local/bin/opencode /usr/local/bin/opencode
COPY requirements.txt /opt/validation/project-requirements.txt
RUN python -m venv --copies /opt/application-python \
    && /opt/application-python/bin/python -m pip install --no-cache-dir pip==25.0.1 setuptools==80.9.0 wheel==0.48.0 packaging==26.3
COPY vibearr_executor /opt/executor/vibearr_executor
COPY vibearr_runner /opt/executor/vibearr_runner
COPY coding_runner /opt/executor/coding_runner
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor \
    && ln -s /opt/executor/vibearr_runner /usr/local/lib/python3.12/site-packages/vibearr_runner \
    && ln -s /opt/executor/coding_runner /usr/local/lib/python3.12/site-packages/coding_runner
WORKDIR /opt/executor
USER 0:0
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.compact_worker"]

FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS runner
LABEL io.vibearr.role="runner"
RUN apt-get update && apt-get install -y --no-install-recommends git gh ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --create-home --home-dir /home/runner runner \
    && useradd --uid 1000 --no-create-home login \
    && mkdir -p /opt/executor /var/lib/vibearr /logins /login-work
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp
COPY --from=runner-binaries /usr/local/bin/codex /usr/local/bin/codex
COPY --from=runner-binaries /usr/local/bin/codex-code-mode-host /usr/local/bin/codex-code-mode-host
COPY --from=runner-binaries /usr/local/bin/claude /usr/local/bin/claude
COPY --from=runner-binaries /usr/local/bin/opencode /usr/local/bin/opencode
COPY vibearr_executor /opt/executor/vibearr_executor
COPY vibearr_runner /opt/executor/vibearr_runner
COPY coding_runner /opt/executor/coding_runner
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor \
    && ln -s /opt/executor/vibearr_runner /usr/local/lib/python3.12/site-packages/vibearr_runner \
    && ln -s /opt/executor/coding_runner /usr/local/lib/python3.12/site-packages/coding_runner
WORKDIR /opt/executor
USER 0:0
ENTRYPOINT ["python", "-I", "-m", "vibearr_runner.compact_connections"]

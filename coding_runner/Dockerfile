FROM node:22-bookworm-slim AS node

FROM python:3.12-slim-bookworm

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG CODEX_VERSION=0.169.0
RUN npm install --global @openai/codex@${CODEX_VERSION} \
    && npm cache clean --force \
    && useradd --create-home --uid 10001 runner \
    && mkdir /data \
    && chown runner:runner /data

WORKDIR /opt/runner
COPY coding_runner /opt/runner/coding_runner
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER runner
ENTRYPOINT ["python", "-m", "coding_runner"]

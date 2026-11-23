# syntax=docker/dockerfile:1
ARG RUNTIME_IMAGE=vibearr-worker-base:local
FROM ${RUNTIME_IMAGE}
LABEL io.vibearr.role="worker"
USER 0:0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN (getent passwd 10002 >/dev/null || useradd --uid 10002 --no-create-home testdatabase) \
    && apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && python -m pip install --no-cache-dir playwright==1.65.2 greenlet==3.5.6 \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /browser-services \
    && chmod 0711 /browser-services
COPY vibearr_executor /opt/executor/vibearr_executor
COPY vibearr_runner /opt/executor/vibearr_runner
RUN ln -sfn /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor \
    && ln -sfn /opt/executor/vibearr_runner /usr/local/lib/python3.12/site-packages/vibearr_runner \
    && python -I -m vibearr_executor.browser_fixture
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.compact_worker"]

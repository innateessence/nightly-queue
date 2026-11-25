ARG SOURCE_IMAGE
ARG WORKER_IMAGE
ARG TOOLS_IMAGE
FROM ${SOURCE_IMAGE} AS source
FROM ${TOOLS_IMAGE} AS tools
FROM ${WORKER_IMAGE}
COPY --from=tools /usr/local/bin/actionlint /usr/local/bin/actionlint
COPY vibearr_runner /opt/executor/vibearr_runner
COPY deploy /opt/executor/deploy
COPY deploy/docker/test_distributions.py /opt/validation/distribution_checks.py
COPY .github/actionlint.yaml /opt/validation/actionlint.yaml
COPY --from=source / /input-source
RUN chmod 0700 /input-source
ENTRYPOINT ["python", "-m", "deploy.docker.packaging_job"]

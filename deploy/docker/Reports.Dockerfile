ARG TOOLS_IMAGE=vibearr-tools:local
FROM ${TOOLS_IMAGE}
COPY --chown=10001:10001 . /reports
USER 10001:10001
ENTRYPOINT ["python", "-m", "deploy.docker.engineering_report", "/reports"]

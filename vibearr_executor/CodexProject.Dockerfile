ARG CODEX_IMAGE
FROM ${CODEX_IMAGE}
COPY vibearr_executor/validation-requirements.txt /opt/vibearr-project-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/vibearr-project-requirements.txt
COPY vibearr_executor /opt/executor/vibearr_executor

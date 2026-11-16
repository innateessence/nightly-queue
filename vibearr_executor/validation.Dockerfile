FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c
LABEL io.vibearr.executor.protocol="1" io.vibearr.validation.profile="vibearr-python312-v1"
RUN apt-get update \
    && apt-get install -y --no-install-recommends git=1:2.39.5-0+deb12u3 iptables=1.8.9-2 ca-certificates=20230311+deb12u1 \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /input /state /work /secrets /home/task /opt/candidate /opt/validation
COPY vibearr_executor/validation-requirements.txt /opt/validation/requirements.txt
COPY requirements.txt /opt/validation/project-requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/validation/requirements.txt
RUN python -m venv --without-pip --copies /opt/application-python
COPY vibearr_executor /opt/executor/vibearr_executor
RUN ln -s /opt/executor/vibearr_executor /usr/local/lib/python3.12/site-packages/vibearr_executor
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/executor
ENTRYPOINT ["python", "-I", "-m", "vibearr_executor.worker"]

# syntax=docker/dockerfile:1
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble@sha256:38dd67fc654b56b3cba611e4c8eb71cd33750e1cd29eea2a5dc00b484817a75f
RUN python -c 'import sys; assert sys.version_info[:2] == (3, 12)' \
    && python -m pip install --no-cache-dir playwright==1.65.2 greenlet==3.5.6
COPY deploy/docker/browser.py /opt/browser.py
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp PYTHONUNBUFFERED=1
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python", "-I", "/opt/browser.py"]

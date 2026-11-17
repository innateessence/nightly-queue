# syntax=docker/dockerfile:1
FROM python:3.12.12-slim-bookworm@sha256:593bd06efe90efa80dc4eee3948be7c0fde4134606dd40d8dd8dbcade98e669c AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/tmp XDG_CACHE_HOME=/tmp/cache RUFF_CACHE_DIR=/tmp/cache/ruff MYPY_CACHE_DIR=/tmp/cache/mypy
WORKDIR /app
RUN useradd --uid 10001 --create-home appuser
COPY requirements.txt /opt/vibearr/requirements.txt
RUN python -m pip install --no-cache-dir -r /opt/vibearr/requirements.txt
COPY . /app
RUN python -m pip install --no-cache-dir --no-deps .
ENTRYPOINT ["python", "-m", "deploy.docker.entrypoint"]

FROM base AS tools
USER root
RUN apt-get update && apt-get install -y --no-install-recommends git postgresql-client ca-certificates \
    && rm -rf /var/lib/apt/lists/*
USER 10001:10001
CMD ["python", "manage.py", "check"]

FROM base AS application
RUN python manage.py collectstatic --noinput --clear
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "vibearr_app.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "90", "--access-logfile", "-", "--error-logfile", "-"]

# syntax=docker/dockerfile:1
FROM python:3.12-slim AS builder

WORKDIR /app

# Use the bundled venv so we don't pollute the system env
ARG PY_ENABLE_SHARED=1
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt


# ---- runtime stage ----
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    HOME="/app"

COPY --from=builder /opt/venv /opt/venv

# Copy the app code
COPY manage.py .
COPY mypy.ini pyproject.toml .
COPY nightly_queue/ nightly_queue/
COPY hello/ hello/

# Create a non-root user (sqlite db lives in /app, owned by appuser)
RUN useradd -m -r appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# Run migrations then start the dev server (fine for dev/boilerplate)
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]

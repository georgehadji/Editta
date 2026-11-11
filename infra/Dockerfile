FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .
# So a deploy can migrate from the image itself: `alembic upgrade head` in a container.
COPY alembic.ini ./
COPY infra/migrations ./infra/migrations

# Never run as root: a bug in manuscript parsing should not own the container.
RUN useradd --system --create-home --uid 10001 editta
USER editta

FROM base AS api
# Liveness only (the process answers); /readyz is for the load balancer. stdlib, no curl in slim.
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"]
CMD ["uvicorn", "editta.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
CMD ["python", "-m", "editta.worker.main"]

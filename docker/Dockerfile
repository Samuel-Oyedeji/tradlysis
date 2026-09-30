# Python image for the engine, API and analyzer processes.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv/tradlysis
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install . && useradd --create-home --uid 10001 tradlysis
USER tradlysis

# Overridden per service in docker-compose.yml
CMD ["python", "-m", "app.engine"]

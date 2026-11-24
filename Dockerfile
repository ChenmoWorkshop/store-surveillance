FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    MONITOR_CLOUD=1 \
    MONITOR_DB=/var/lib/monitor/monitor.db

WORKDIR /app
COPY requirements.txt ./
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && pip install --no-cache-dir -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY monitor/ ./monitor/
COPY web/ ./web/
EXPOSE 8080
CMD ["python", "-m", "uvicorn", "monitor.app:app", "--host", "0.0.0.0", "--port", "8080"]

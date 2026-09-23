# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Berlin \
    DB_PATH=/data/poc.db \
    CONFIG_PATH=/app/config.yaml

# gosu: der Container startet als root, legt die Rechte auf dem gemounteten
# ./data-Verzeichnis zurecht und wechselt dann auf den unprivilegierten User.
# tzdata: der Scheduler rechnet in Europe/Berlin.
RUN apt-get update \
 && apt-get install -y --no-install-recommends gosu tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

RUN groupadd --system app && useradd --system --gid app --home /app app

COPY app ./app
COPY config.yaml ./config.yaml
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

VOLUME ["/data"]
EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]

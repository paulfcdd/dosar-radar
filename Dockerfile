FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    CETATENIE_DB=/data/data.sqlite3

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py cli.py ./
COPY cetatenie/ ./cetatenie/
COPY templates/ ./templates/

# База лежить у томі, тому власником має бути непривілейований користувач:
# SQLite у режимі WAL пише не лише сам файл, а й -wal/-shm поруч із ним.
RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data \
    && chown -R app:app /data /app
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/').read(1)"

# Один воркер із кількома потоками — свідомо: прогрес фонової синхронізації
# живе в пам'яті процесу, тож із кількома воркерами /sync/status відповідав би
# то з того процесу, що качає PDF, то з іншого, який про це не знає.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "1", "--threads", "8", \
     "--timeout", "120", "--access-logfile", "-", \
     "app:app"]

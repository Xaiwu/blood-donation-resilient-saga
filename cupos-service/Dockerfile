FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    DATABASE_PATH=/app/data/cupos.db

WORKDIR /app

RUN groupadd -r appuser && useradd -r -g appuser appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY db/ ./db/
COPY src/ ./src/

RUN mkdir -p /app/data && chown -R appuser:appuser /app

USER appuser

EXPOSE 5001

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5001"]

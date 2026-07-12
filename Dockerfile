FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY src /app/src
RUN pip install --no-cache-dir /app

USER 65532:65532
CMD ["sh", "-c", "uvicorn gcs_search_macro_v4.api:app --host 0.0.0.0 --port ${PORT}"]

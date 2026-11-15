FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    "fastapi==0.163.1" \
    "uvicorn==0.54.0" \
    "httpx==0.28.1" \
    "prometheus-client==0.26.0"

COPY ollama_exporter.py .

# Hide the noisy *_created series emitted by prometheus_client
ENV PROMETHEUS_DISABLE_CREATED_SERIES=True \
    PYTHONUNBUFFERED=1

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=3)"

CMD ["uvicorn", "ollama_exporter:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

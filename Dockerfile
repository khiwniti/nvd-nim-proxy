FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PROXY_HOST=0.0.0.0

WORKDIR /app

RUN addgroup --system app && adduser --system --ingroup app app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py .
COPY nim_code.py .
COPY config.example.yaml .

USER app

EXPOSE 8080

# Cloudflare Containers and Cloud Run inject PORT; proxy.py reads PROXY_PORT.
CMD ["sh", "-c", "PROXY_PORT=${PORT:-8080} python proxy.py"]

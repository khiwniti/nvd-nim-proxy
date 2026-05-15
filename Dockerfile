FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY proxy.py .
COPY nim_code.py .
COPY config.example.yaml .

ENV PROXY_HOST=0.0.0.0

EXPOSE 8080

# Cloud Run injects PORT; proxy.py reads PROXY_PORT
CMD ["sh", "-c", "PROXY_PORT=${PORT:-8080} python proxy.py"]

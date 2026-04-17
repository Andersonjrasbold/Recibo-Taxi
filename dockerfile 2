# syntax=docker/dockerfile:1
FROM python:3.12-slim

# 1) Variáveis úteis
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8080

WORKDIR /app

# 2) Instala deps de build só se precisar (psycopg / pillow etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

# 3) Dependências Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 4) Copia o app
COPY . .

# Usuário não-root
RUN useradd -m appuser
USER appuser

EXPOSE 8080
CMD ["gunicorn","-w","1","-k","gthread","--threads","8","--access-logfile","-","--error-logfile","-","--log-level","debug","-b","0.0.0.0:8080","app:app"]

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

# 5) Usuário não-root (boa prática)
RUN useradd -m appuser
USER appuser

# 6) Exponha a porta (tem que bater com fly.toml)
EXPOSE 8080

# 7) Start com gunicorn
#   - "app:app" = <arquivo_python>:<objeto_flask>
#   - Se seu entrypoint for outro (ex: wsgi:app), ajuste aqui.
CMD ["gunicorn", "-w", "4", "-k", "gthread", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "debug", "-b", "0.0.0.0:8080", "app:app"]

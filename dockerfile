# syntax=docker/dockerfile:1
FROM python:3.12-slim

# Configurações básicas do Python (logs imediatos e sem .pyc)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instale as dependências primeiro (melhora cache)
# Certifique-se de ter um requirements.txt na raiz do repo
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copie o restante do código
COPY . .

# Exponha a porta que o app usa
EXPOSE 8080

# Inicie com gunicorn (4 workers, thread worker, porta 8080)
# Ajuste "app:app" se seu arquivo/instância tiver outro nome
CMD ["gunicorn", "-w", "4", "-k", "gthread", "-b", "0.0.0.0:8080", "app:app"]

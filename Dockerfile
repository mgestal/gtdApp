FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
	PYTHONUNBUFFERED=1 \
	PIP_NO_CACHE_DIR=1

WORKDIR /app

# Instalar dependencias Python aprovechando la cache de capas.
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install -r requirements.txt

# Copiar la aplicación.
COPY . .

RUN chmod +x docker-entrypoint.sh && mkdir -p instance backups

EXPOSE 5000

ENTRYPOINT ["./docker-entrypoint.sh"]

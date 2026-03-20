FROM python:3.10-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y gcc default-libmysqlclient-dev && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar app
COPY . .

# Crear directorio instance si no existe
RUN mkdir -p instance

EXPOSE 5000

# Copiar config de Docker y ejecutar app
CMD ["sh", "-c", "cp instance/config.docker.json instance/config.json && gunicorn --bind 0.0.0.0:5000 wsgi:app"]

# Imagen compartida por la API (uvicorn), el worker (sync_task) y el servicio one-off
# `migrate` (alembic) — ver docker-compose.yml para el comando que arranca cada uno.
FROM python:3.14-slim

WORKDIR /app

# build-essential: algunas dependencias (p. ej. asyncpg) pueden requerir compilar desde
# fuente si aun no existe wheel prebuilt para esta version de Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

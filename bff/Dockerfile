FROM python:3.12-slim

# Evitar escritura de archivos .pyc y forzar salida stdout/stderr sin buffer
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5000

WORKDIR /app

# Crear usuario y grupo no-root para ejecución segura
RUN groupadd -r appuser && useradd -r -g appuser -d /app -s /sbin/nologin appuser

# Instalar dependencias
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código fuente
COPY src/ ./src/

# Asignar permisos al usuario no-root
RUN chown -R appuser:appuser /app

USER appuser

EXPOSE 5000

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "5000"]


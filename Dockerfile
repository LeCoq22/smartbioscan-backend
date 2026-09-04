FROM python:3.11-slim-bookworm

WORKDIR /app

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium y todas sus dependencias del sistema.
RUN playwright install chromium --with-deps

# Copiar el código
COPY . .

# Puerto que Railway asigna dinámicamente
ENV PORT=8000
EXPOSE $PORT

CMD ["sh", "-c", "python -m uvicorn api.main:app --host 0.0.0.0 --port $PORT"]

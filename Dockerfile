FROM python:3.11-slim-bullseye

# Instalar dependencias del sistema para Playwright y pdfkit
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    && rm -rf /var/lib/apt/lists/*

RUN apt-get update && apt-get install -y wget && \
    wget https://github.com/wkhtmltopdf/packaging/releases/download/0.12.6.1-2/wkhtmltox_0.12.6.1-2.bullseye_amd64.deb && \
    apt-get install -y ./wkhtmltox_0.12.6.1-2.bullseye_amd64.deb && \
    rm wkhtmltox_0.12.6.1-2.bullseye_amd64.deb && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copiar e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Playwright y browsers
RUN playwright install chromium --with-deps

# Copiar el código
COPY . .

# Puerto que Railway asigna dinámicamente
ENV PORT=8000
EXPOSE $PORT

CMD ["sh", "-c", "python -m uvicorn api.main:app --host 0.0.0.0 --port $PORT"]

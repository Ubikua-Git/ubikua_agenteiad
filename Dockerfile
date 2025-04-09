# 1. Usar una imagen oficial de Python como base
FROM python:3.11-slim

# Establecer variables de entorno para Python
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# 2. Instalar Tesseract OCR y el paquete de idioma español
RUN apt-get update && \
    apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 3. Establecer el directorio de trabajo dentro del contenedor
WORKDIR /app

# 4. Copiar el archivo de requerimientos e instalar dependencias Python
# Copiar primero para aprovechar el caché de Docker si los requerimientos no cambian
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Copiar el resto del código de la aplicación
COPY . .

# 6. Indicar el puerto que la aplicación escuchará (Render usará $PORT en el Start Command)
# EXPOSE 8000 # Opcional, más informativo que funcional en Render

# 7. Comando por defecto para ejecutar la app (Render probablemente lo sobreescribirá con el Start Command de la UI)
# CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
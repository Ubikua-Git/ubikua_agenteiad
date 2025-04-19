# --- INICIO config.py v2.4.4‑mt‑refactor (Refactor modularización) ---
"""
config.py

Este módulo centraliza la configuración de logging y la creación de la instancia FastAPI,
extraída de main.py para facilitar la modularización y futuros updates independientes.

Changelog:
- v2.4.4‑mt‑refactor (19‑04‑2025):  
  • Extraída la configuración de logging.  
  • Inicialización del cliente OpenAI.  
  • Creación de la app FastAPI con metadatos actualizados.
"""

import os
import logging
from fastapi import FastAPI
from openai import OpenAI

# Configuración del Logging
logging.basicConfig(
    level=logging.INFO,  # Cambiar a DEBUG para más detalle durante el desarrollo
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# Inicializar cliente OpenAI
openai_api_key = os.getenv("OPENAI_API_KEY")
if not openai_api_key:
    logger.warning("Variable OPENAI_API_KEY no encontrada. Funcionalidad IA limitada.")
    client = None
else:
    try:
        client = OpenAI(api_key=openai_api_key)
        logger.info("Cliente OpenAI inicializado correctamente.")
    except Exception as err:
        logger.error(f"Error al inicializar cliente OpenAI: {err}")
        client = None

# Crear instancia FastAPI
app = FastAPI(
    title="Asistente IA UBIKUA API v2.4.4‑mt",
    version="2.4.4‑mt",
    description="API de UBIKUA tras extraer la configuración a config.py"
)

# CORS u otros middlewares globales se añadirán en main.py o en routers especializados
# --- FIN config.py ---

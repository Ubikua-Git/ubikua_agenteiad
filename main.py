from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests
import base64 # <--- Añadido para codificar imágenes
import logging
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError

# Configurar logging básico
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(
    title="Asistente IA Ashotel API v2 (con GPT-4V)",
    description="API para consultas y análisis de documentos (incluyendo imágenes vía GPT-4V) para Ashotel",
    version="2.0.0" # Incremento versión por cambio mayor
)

# Configuración CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Ajustar en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración de Clientes y API Keys ---
try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("Variable de entorno OPENAI_API_KEY no encontrada.")
    client = OpenAI(api_key=openai_api_key)
    logging.info("Cliente OpenAI inicializado.")

    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Variables de entorno GOOGLE_API_KEY o GOOGLE_CX no encontradas. La búsqueda web no funcionará.")
        GOOGLE_API_KEY = None
        GOOGLE_CX = None

except ValueError as e:
    logging.error(f"Error de configuración inicial: {e}")
    client = None
except Exception as e:
    logging.error(f"Error inesperado al inicializar clientes: {e}")
    client = None
    GOOGLE_API_KEY = None
    GOOGLE_CX = None

# --- Modelos de Datos (Pydantic) ---
class PeticionConsulta(BaseModel):
    mensaje: str = Field(..., min_length=1)
    especializacion: str = Field(default="general")
    buscar_web: bool = Field(default=False)

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

# --- Prompts y Configuraciones IA ---
# Prompts base (pueden ser los mismos o ajustados)
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de Ashotel, la Asociación Hotelera y Extrahotelera de Tenerife, La Palma, La Gomera y El Hierro. "
    "Tu misión es ayudar a los distintos equipos internos de Ashotel con respuestas claras, precisas, y alineadas a sus objetivos estratégicos. "
    "Si no tienes información directa sobre temas muy específicos o actuales, indícalo claramente y, si se te proporciona contexto web, intégralo. "
    "Cuando respondas con listas estructuradas o datos comparativos, utiliza siempre tablas en formato HTML (usa <table>, <thead>, <tbody>, <tr>, <th>, <td>). "
    "Para listas simples, usa <ul> y <li>. Para enfatizar, usa <strong> o <em>. "
    "Evita usar Markdown (como asteriscos para negrita o guiones para listas). Tu respuesta debe ser directamente HTML renderizable."
)
# Prompt base para informes (ajustado ligeramente)
BASE_PROMPT_ANALISIS_DOC = (
    "Eres el Asistente IA oficial de Ashotel, experto en redactar informes profesionales concisos y claros "
    "a partir de contenido textual o visual de documentos (PDF, DOCX, imágenes). "
    "Estructura siempre los informes con claridad, estilo formal y formato HTML limpio. "
    "Usa encabezados (<h2>, <h3>), párrafos (<p>), listas (<ul>, <li>), y énfasis (<strong>, <em>) apropiadamente. "
    "La respuesta debe ser únicamente el código HTML del informe, sin explicaciones previas o posteriores. "
    "Adapta ligeramente el tono y enfoque según la especialización indicada."
)

PROMPT_ESPECIALIZACIONES = {
    "general": "Actúa como un asistente generalista, capaz de abordar una amplia variedad de temas relacionados con Ashotel y el sector.",
    "legal": "Enfócate en la perspectiva legal. Analiza implicaciones normativas, resume textos legales y usa terminología jurídica precisa.",
    "comunicacion": "Adopta un rol de experto en comunicación. Enfócate en mensajes clave, redacción clara, y posibles implicaciones para la imagen pública.",
    "formacion": "Actúa como especialista en formación. Identifica puntos clave para la enseñanza, sugiere estructuras didácticas y usa lenguaje pedagógico.",
    "informatica": "Enfócate en los aspectos técnicos. Analiza requisitos, resume especificaciones o identifica problemas/soluciones tecnológicas.",
    "direccion": "Adopta una perspectiva estratégica y de gestión. Resume puntos clave para la toma de decisiones y analiza el impacto organizacional.",
    "innovacion": "Enfócate en la novedad y la transformación digital. Identifica tendencias, oportunidades de mejora y tecnologías emergentes.",
    "contabilidad": "Actúa como experto en contabilidad/finanzas. Analiza datos económicos, resume información financiera y presta atención a cifras clave.",
    "administracion": "Enfócate en la eficiencia de procesos y la gestión administrativa. Resume procedimientos, identifica puntos de mejora organizativa.",
}

FRASES_BUSQUEDA = [
    "no tengo información", "no dispongo de información", "no estoy seguro",
    "no encontré datos", "no tengo acceso a información en tiempo real",
    "mi conocimiento es limitado hasta", "como modelo de lenguaje, no puedo saber"
]

# Carpeta temporal
TEMP_DIR = "/tmp/uploads_ashotel"
os.makedirs(TEMP_DIR, exist_ok=True)
logging.info(f"Directorio temporal creado/asegurado: {TEMP_DIR}")

# --- Funciones Auxiliares ---

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    """Extrae texto SOLO de PDF y DOCX."""
    texto = ""
    logging.info(f"Extrayendo texto de: {ruta_archivo} (Ext: {extension})")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo)
                if lector.is_encrypted:
                    logging.warning(f"El PDF {ruta_archivo} está encriptado.")
                    # lector.decrypt('') # Descomentar si se necesita manejar PDFs con contraseña vacía
                for pagina in lector.pages:
                    texto_pagina = pagina.extract_text()
                    if texto_pagina:
                        texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]:
            doc = Document(ruta_archivo)
            texto = "\n".join([parrafo.text for parrafo in doc.paragraphs if parrafo.text])
        else:
             logging.warning(f"Llamada inesperada a extraer_texto_pdf_docx para extensión: {extension}")
             return "" # No debería llamarse para otros tipos

        logging.info(f"Texto extraído con éxito (longitud: {len(texto)} caracteres).")
        return texto.strip()

    except pdf_errors.PdfReadError as e:
        logging.error(f"Error al leer PDF {ruta_archivo}: {e}")
        return "[Error: No se pudo leer el archivo PDF, puede estar dañado o protegido.]"
    except PackageNotFoundError:
        logging.error(f"Error: El archivo {ruta_archivo} no parece ser un DOCX válido.")
        return "[Error: El archivo no es un formato DOCX válido.]"
    except FileNotFoundError:
         logging.error(f"Error: Archivo temporal no encontrado en {ruta_archivo}.")
         return "[Error interno: Archivo temporal no encontrado]"
    except Exception as e:
        logging.error(f"Error inesperado al extraer texto de PDF/DOCX {ruta_archivo}: {e}")
        return "[Error interno procesando el archivo PDF/DOCX.]"

def buscar_google(query: str) -> str:
    """Realiza una búsqueda en Google y devuelve los 3 primeros resultados como HTML."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Intento de búsqueda web sin claves API de Google configuradas.")
        return "<p><i>[Búsqueda web no disponible en la configuración actual del servidor.]</i></p>"
    # ... (resto de la función buscar_google igual que antes) ...
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3} # Pedir 3 resultados
    logging.info(f"Realizando búsqueda web para: '{query}'")
    try:
        response = requests.get(url, params=params, timeout=10) # Añadir timeout
        response.raise_for_status()  # Lanza excepción para errores HTTP 4xx/5xx
        data = response.json()
        resultados = data.get("items", [])
        if not resultados:
            logging.info("Búsqueda web no devolvió resultados.")
            return "<p><i>[No se encontraron resultados relevantes en la web.]</i></p>"
        texto_resultados = "<div class='google-results' style='margin-top: 15px; border-top: 1px solid #eee; padding-top: 10px;'>"
        texto_resultados += "<h4 style='font-size: 0.9em; color: #555; margin-bottom: 8px;'>Resultados de búsqueda web:</h4>"
        for item in resultados:
            title = item.get('title', 'Sin título')
            link = item.get('link', '#')
            snippet = item.get('snippet', 'Sin descripción').replace('\n', ' ')
            texto_resultados += (
                f"<div class='result-item' style='margin-bottom: 10px; font-size: 0.85em;'>"
                f"<a href='{link}' target='_blank' style='color: #1a0dab; text-decoration: none; font-weight: bold;'>{title}</a>"
                f"<p style='color: #545454; margin-top: 2px; margin-bottom: 2px;'>{snippet}</p>"
                f"<cite style='color: #006621; font-style: normal; font-size: 0.9em;'>{link}</cite>"
                f"</div>\n"
            )
        texto_resultados += "</div>"
        logging.info(f"Búsqueda web exitosa, {len(resultados)} resultados formateados.")
        return texto_resultados
    except requests.exceptions.Timeout:
        logging.error("Error en búsqueda web: Timeout.")
        return "<p><i>[Error: La búsqueda web tardó demasiado en responder.]</i></p>"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error en búsqueda web: {e}")
        return f"<p><i>[Error al conectar con el servicio de búsqueda web: {e}]</i></p>"
    except Exception as e:
        logging.error(f"Error inesperado durante búsqueda web: {e}")
        return "<p><i>[Error inesperado durante la búsqueda web.]</i></p>"

# --- Endpoints de la API ---

@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    """Recibe una consulta y devuelve la respuesta generada por la IA."""
    if not client:
         logging.error("Intento de consulta sin cliente OpenAI inicializado.")
         raise HTTPException(status_code=503, detail="Servicio IA no disponible (Error de configuración).")
    # ... (resto de la función /consulta igual que antes) ...
    especializacion = datos.especializacion.lower()
    mensaje_usuario = datos.mensaje
    forzar_busqueda_web = datos.buscar_web
    logging.info(f"Recibida consulta: Especialización='{especializacion}', BuscarWebForzado={forzar_busqueda_web}")
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt = f"{BASE_PROMPT_CONSULTA}\n{prompt_especifico}"
    texto_respuesta_final = ""
    activar_busqueda = forzar_busqueda_web
    web_resultados_html = ""
    try:
        logging.info("Realizando primera llamada a OpenAI...")
        respuesta_inicial = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": mensaje_usuario}
            ],
            temperature=0.5,
            max_tokens=1500
        )
        texto_respuesta_inicial = respuesta_inicial.choices[0].message.content.strip()
        logging.info("Primera respuesta de OpenAI recibida.")
        if not activar_busqueda:
            respuesta_lower = texto_respuesta_inicial.lower()
            if any(frase in respuesta_lower for frase in FRASES_BUSQUEDA):
                logging.info("Respuesta inicial indica falta de información. Activando búsqueda web.")
                activar_busqueda = True
        if activar_busqueda:
            logging.info("Iniciando búsqueda web...")
            web_resultados_html = buscar_google(mensaje_usuario)
            if "[Error" not in web_resultados_html:
                 logging.info("Realizando segunda llamada a OpenAI con contexto web...")
                 mensaje_con_contexto = (
                     f"Consulta original: {mensaje_usuario}\n\n"
                     f"Contexto adicional obtenido de una búsqueda web:\n{web_resultados_html}\n\n"
                     "Por favor, responde a la consulta original integrando la información relevante del contexto web."
                 )
                 respuesta_con_contexto = client.chat.completions.create(
                     model="gpt-4-turbo",
                     messages=[
                         {"role": "system", "content": system_prompt},
                         {"role": "user", "content": mensaje_con_contexto}
                     ],
                     temperature=0.5,
                     max_tokens=1500
                 )
                 texto_respuesta_final = respuesta_con_contexto.choices[0].message.content.strip()
                 logging.info("Segunda respuesta de OpenAI (con contexto) recibida.")
            else:
                 logging.warning("Búsqueda web falló, se usará la respuesta inicial con nota de error.")
                 texto_respuesta_final = texto_respuesta_inicial + "\n" + web_resultados_html
        else:
             texto_respuesta_final = texto_respuesta_inicial
    except APIError as e:
        logging.error(f"Error de API OpenAI en /consulta: {e}")
        raise HTTPException(status_code=503, detail=f"Error al contactar el servicio IA: {e.message}")
    except Exception as e:
        logging.error(f"Error inesperado en /consulta: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor al procesar la consulta.")
    return RespuestaConsulta(respuesta=texto_respuesta_final)


@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(..., description="Archivo a analizar (PDF, DOCX, PNG, JPG, WEBP, GIF)."),
    especializacion: str = Form(default="general", description="Área de especialización para enfocar el informe.")
):
    """
    Recibe un archivo (imagen, PDF, DOCX), extrae contenido (usando GPT-4V para imágenes)
    y genera un informe HTML con IA.
    """
    if not client:
         logging.error("Intento de análisis sin cliente OpenAI inicializado.")
         raise HTTPException(status_code=503, detail="Servicio IA no disponible (Error de configuración).")

    filename = file.filename or "unknown_file"
    content_type = file.content_type or "application/octet-stream"
    extension = filename.split('.')[-1].lower() if '.' in filename else ''
    logging.info(f"Recibido archivo: {filename}, Tipo: {content_type}")

    # Construir prompt de sistema base para el informe
    especializacion_lower = especializacion.lower()
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt = f"{BASE_PROMPT_ANALISIS_DOC}\n{prompt_especifico}"

    informe_html = ""
    messages_payload = [] # Payload para la API de OpenAI

    # Tipos MIME comunes para imágenes
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]

    try:
        # --- Caso 1: Archivo es una IMAGEN ---
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando archivo como IMAGEN ({content_type}).")
            # Leer bytes de la imagen directamente desde UploadFile
            image_bytes = await file.read()
            # Codificar en base64
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            logging.info(f"Imagen codificada en base64 (longitud: {len(base64_image)}).")

            # Prompt específico para imágenes pidiendo OCR + Informe
            user_prompt_image = (
                "Analiza la siguiente imagen. Extrae todo el texto relevante que contiene (actúa como OCR). "
                "Basándote en el texto extraído de la imagen, redacta un informe profesional claro y estructurado en formato HTML. "
                "Sigue las instrucciones de formato HTML (<h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>) y evita Markdown. "
                "Devuelve únicamente el código HTML del informe, sin explicaciones adicionales."
            )

            # Construir payload multimodal para OpenAI
            messages_payload = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": user_prompt_image},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{content_type};base64,{base64_image}",
                                # "detail": "low" # Opcional: usar 'low' para reducir coste si la alta resolución no es crítica
                            }
                        }
                    ]
                }
            ]
            logging.info("Payload multimodal para OpenAI construido.")

        # --- Caso 2: Archivo es PDF o DOCX ---
        elif extension in ["pdf", "docx", "doc"]:
            logging.info(f"Procesando archivo como PDF/DOCX ({extension}).")
            ruta_temporal = os.path.join(TEMP_DIR, f"upload_{os.urandom(8).hex()}.{extension}")

            # Guardar temporalmente para leer con PyPDF2/python-docx
            try:
                with open(ruta_temporal, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                logging.info(f"Archivo guardado temporalmente en: {ruta_temporal}")
                texto_extraido = extraer_texto_pdf_docx(ruta_temporal, extension)
            finally:
                # Asegurarse de borrar el temporal aunque falle la extracción
                if os.path.exists(ruta_temporal):
                    try:
                        os.remove(ruta_temporal)
                        logging.info(f"Archivo temporal eliminado: {ruta_temporal}")
                    except OSError as e:
                         logging.error(f"Error al eliminar archivo temporal {ruta_temporal}: {e}")

            # Verificar si hubo error en la extracción
            if texto_extraido.startswith("[Error"):
                logging.warning(f"Extracción de texto fallida para {filename}. Razón: {texto_extraido}")
                return RespuestaAnalisis(informe=f"<p class='text-red-500'>{texto_extraido}</p>")
            elif not texto_extraido:
                 logging.warning(f"No se extrajo texto del archivo PDF/DOCX {filename}.")
                 return RespuestaAnalisis(informe="<p class='text-orange-500'>[Advertencia: No se pudo extraer contenido textual del archivo PDF/DOCX.]</p>")

            logging.info(f"Texto extraído de PDF/DOCX (longitud: {len(texto_extraido)}).")

            # Prompt específico para texto extraído
            user_prompt_text = (
                "Redacta un informe profesional claro y estructurado en formato HTML basado únicamente en el siguiente texto extraído de un documento. "
                "Sigue las instrucciones de formato HTML (<h2>, <h3>, <p>, <ul>, <li>, <strong>, <em>) y evita Markdown.\n\n"
                "--- INICIO TEXTO EXTRAÍDO ---\n"
                f"{texto_extraido}\n"
                "--- FIN TEXTO EXTRAÍDO ---\n\n"
                "Recuerda: Devuelve solo el HTML del informe."
            )

            # Construir payload de texto para OpenAI
            messages_payload = [
                 {"role": "system", "content": system_prompt},
                 {"role": "user", "content": user_prompt_text}
            ]
            logging.info("Payload de texto para OpenAI construido.")

        # --- Caso 3: Tipo de archivo no soportado ---
        else:
            logging.warning(f"Tipo de archivo no soportado: {filename} (Tipo MIME: {content_type}, Ext: {extension})")
            raise HTTPException(status_code=415, detail=f"Tipo de archivo no soportado: {content_type or extension}. Solo se aceptan PDF, DOCX, PNG, JPG, WEBP, GIF.")

        # --- Llamada a OpenAI ---
        if messages_payload: # Asegurarse que el payload se construyó
            logging.info(f"Realizando llamada a OpenAI con el modelo {client.models.list().data[0].id if client else 'unknown'}...") # Ajustar si usas un modelo específico
            respuesta_informe = client.chat.completions.create(
                # Asegúrate que el modelo soporta visión, gpt-4-turbo lo hace.
                # Puedes especificar 'gpt-4-vision-preview' si quieres ser explícito,
                # pero 'gpt-4-turbo' es generalmente el recomendado y más reciente.
                model="gpt-4-turbo",
                messages=messages_payload,
                temperature=0.3,
                max_tokens=2500 # Ajustar según necesidad
            )
            informe_html = respuesta_informe.choices[0].message.content.strip()
            logging.info(f"Informe generado con éxito para {filename}.")

            # Limpieza básica del HTML (opcional, por si IA añade texto extra)
            if "<!DOCTYPE html>" in informe_html or "<html" in informe_html:
                 # Intentar extraer solo el body si devuelve un documento completo
                 try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(informe_html, 'html.parser')
                    body_content = soup.body.decode_contents() if soup.body else informe_html
                    informe_html = body_content
                    logging.info("HTML completo detectado, extraído contenido del body.")
                 except ImportError:
                    logging.warning("BeautifulSoup no instalado, no se pudo extraer body de HTML completo.")
                 except Exception as e:
                    logging.error(f"Error al procesar HTML completo: {e}")

            elif not informe_html.strip().startswith('<'): # Si no empieza con etiqueta, envolver
                 informe_html = f"<p>{informe_html}</p>"

        else:
             # Esto no debería ocurrir si la lógica anterior es correcta
             logging.error("Error inesperado: No se construyó el payload para OpenAI.")
             raise HTTPException(status_code=500, detail="Error interno al preparar la solicitud a la IA.")

    except APIError as e:
        logging.error(f"Error de API OpenAI en /analizar-documento para {filename}: {e}")
        raise HTTPException(status_code=503, detail=f"Error al contactar el servicio IA para generar el informe: {e.message}")
    except HTTPException as e:
        # Re-lanzar excepciones HTTP que ya hemos lanzado (ej. 415)
        raise e
    except Exception as e:
        logging.error(f"Error inesperado en /analizar-documento para {filename}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor al analizar el documento.")
    finally:
        # Asegurarse de cerrar el archivo subido si no se leyó completamente antes
        await file.close()

    return RespuestaAnalisis(informe=informe_html)


# --- Punto de Entrada (Opcional, para debug local) ---
# if __name__ == "__main__":
#     import uvicorn
#     # Para probar localmente, necesitarás un archivo .env con las claves API
#     # from dotenv import load_dotenv
#     # load_dotenv()
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests
import base64
import logging
import pymysql # <-- Añadido
import pymysql.cursors # <-- Añadido
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError

# Configurar logging básico
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(
    title="Asistente IA Ashotel API v2.1 (Personalizado)",
    description="API para consultas y análisis de documentos con prompts personalizados por usuario.",
    version="2.1.0"
)

# Configuración CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Ajustar en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración Clientes, API Keys y BD ---
try:
    # OpenAI
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key: raise ValueError("Variable OPENAI_API_KEY no encontrada.")
    client = OpenAI(api_key=openai_api_key)
    logging.info("Cliente OpenAI inicializado.")

    # Google Search (Opcional)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX: logging.warning("Variables GOOGLE_API_KEY/GOOGLE_CX no encontradas.")

    # --- NUEVO: Credenciales Base de Datos ---
    DB_HOST = os.getenv("DB_HOST")
    DB_USER = os.getenv("DB_USER")
    DB_PASS = os.getenv("DB_PASS")
    DB_NAME = os.getenv("DB_NAME")
    DB_PORT = int(os.getenv("DB_PORT", 3306)) # Puerto MySQL/MariaDB por defecto

    if not DB_HOST or not DB_USER or not DB_PASS or not DB_NAME:
        logging.warning("Faltan variables de entorno DB (DB_HOST, DB_USER, DB_PASS, DB_NAME). No se leerán prompts personalizados.")
        DB_CONFIGURED = False
    else:
        DB_CONFIGURED = True
        logging.info("Credenciales de Base de Datos cargadas.")

except ValueError as e:
    logging.error(f"Error de configuración inicial: {e}")
    client = None; DB_CONFIGURED = False # Marcar como no configurado si falla
except Exception as e:
    logging.error(f"Error inesperado al inicializar: {e}")
    client = None; GOOGLE_API_KEY = None; GOOGLE_CX = None; DB_CONFIGURED = False # Marcar como no configurado si falla


# --- Modelos de Datos (Pydantic) ---
class PeticionConsulta(BaseModel):
    mensaje: str = Field(..., min_length=1)
    especializacion: str = Field(default="general")
    buscar_web: bool = Field(default=False)
    user_id: int | None = None # <-- AÑADIDO user_id opcional

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

# --- Prompts y Configuraciones IA ---
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de Ashotel, la Asociación Hotelera y Extrahotelera de Tenerife, La Palma, La Gomera y El Hierro. "
    "Tu misión es ayudar a los distintos equipos internos de Ashotel con respuestas claras, precisas, y alineadas a sus objetivos estratégicos. "
    "Si no tienes información directa sobre temas muy específicos o actuales, indícalo claramente y, si se te proporciona contexto web, intégralo. "
    "Cuando respondas con listas estructuradas o datos comparativos, utiliza siempre tablas en formato HTML (usa <table>, <thead>, <tbody>, <tr>, <th>, <td>). "
    "Para listas simples, usa <ul> y <li>. Para enfatizar, usa <strong> o <em>. "
    "Evita usar Markdown. Tu respuesta debe ser directamente HTML renderizable."
    # El prompt personalizado se añadirá después si existe
)
BASE_PROMPT_ANALISIS_DOC = (
    "Eres el Asistente IA oficial de Ashotel, experto en redactar informes profesionales concisos y claros "
    "a partir de contenido textual o visual de documentos (PDF, DOCX, imágenes). "
    "Estructura siempre los informes con claridad, estilo formal y formato HTML limpio. "
    "Usa encabezados (<h2>, <h3>), párrafos (<p>), listas (<ul>, <li>), y énfasis (<strong>, <em>) apropiadamente. "
    "La respuesta debe ser únicamente el código HTML del informe, sin explicaciones previas o posteriores. "
    "Adapta ligeramente el tono y enfoque según la especialización indicada."
     # El prompt personalizado se añadirá después si existe
)
PROMPT_ESPECIALIZACIONES = {
    "general": "Actúa como un asistente generalista.",
    "legal": "Enfócate en la perspectiva legal y normativa.",
    "comunicacion": "Adopta un rol de experto en comunicación y marketing.",
    "formacion": "Actúa como especialista en formación y pedagogía.",
    "informatica": "Enfócate en los aspectos técnicos y de sistemas.",
    "direccion": "Adopta una perspectiva estratégica y de gestión.",
    "innovacion": "Enfócate en la novedad y la transformación digital.",
    "contabilidad": "Actúa como experto en contabilidad y finanzas.",
    "administracion": "Enfócate en la eficiencia de procesos administrativos.",
}
FRASES_BUSQUEDA = [
    "no tengo información", "no dispongo de información", "no estoy seguro",
    "no encontré datos", "no tengo acceso a información en tiempo real",
    "mi conocimiento es limitado hasta", "como modelo de lenguaje, no puedo saber"
]

# Carpeta temporal
TEMP_DIR = "/tmp/uploads_ashotel"
os.makedirs(TEMP_DIR, exist_ok=True)
logging.info(f"Directorio temporal: {TEMP_DIR}")

# --- Funciones Auxiliares ---

# NUEVO: Función para conectar a la BD
def get_db_connection():
    if not DB_CONFIGURED:
        logging.warning("Intento de conexión a BD fallido: Configuración incompleta.")
        return None
    try:
        conn = pymysql.connect(
            host=DB_HOST, user=DB_USER, password=DB_PASS, database=DB_NAME,
            port=DB_PORT, cursorclass=pymysql.cursors.DictCursor,
            charset='utf8mb4', connect_timeout=5
        )
        return conn
    except pymysql.MySQLError as e:
        logging.error(f"Error al conectar a la BD ({DB_HOST}:{DB_PORT}): {e}")
        return None

# Función extraer_texto_pdf_docx (sin cambios respecto a v2.0.0)
def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    # ... (pega aquí el código completo de esta función de tu versión anterior) ...
    texto = ""
    logging.info(f"Extrayendo texto de: {ruta_archivo} (Ext: {extension})")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo);
                if lector.is_encrypted: logging.warning(f"PDF encriptado: {ruta_archivo}")
                for pagina in lector.pages:
                    texto_pagina = pagina.extract_text();
                    if texto_pagina: texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]:
            doc = Document(ruta_archivo)
            texto = "\n".join([p.text for p in doc.paragraphs if p.text])
        else: return "" # No debería llegar aquí
        logging.info(f"Texto extraído (longitud: {len(texto)}).")
        return texto.strip()
    except pdf_errors.PdfReadError as e: return f"[Error PDF: {e}]"
    except PackageNotFoundError: return "[Error DOCX: Archivo inválido]"
    except FileNotFoundError: return "[Error interno: Archivo temporal no encontrado]"
    except Exception as e: logging.error(f"Error extraer PDF/DOCX {ruta_archivo}: {e}"); return "[Error interno procesando PDF/DOCX.]"


# Función buscar_google (sin cambios respecto a v2.0.0)
def buscar_google(query: str) -> str:
    # ... (pega aquí el código completo de esta función de tu versión anterior) ...
    if not GOOGLE_API_KEY or not GOOGLE_CX: return "<p><i>[Búsqueda web no disponible.]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3}
    logging.info(f"Buscando en Google: '{query}'")
    try:
        response = requests.get(url, params=params, timeout=10); response.raise_for_status()
        data = response.json(); resultados = data.get("items", [])
        if not resultados: return "<p><i>[No se encontraron resultados web.]</i></p>"
        texto_resultados = "<div class='google-results' style='margin-top:15px;border-top:1px solid #eee;padding-top:10px;'><h4 style='font-size:0.9em;color:#555;margin-bottom:8px;'>Resultados web:</h4>"
        for item in resultados:
            title = item.get('title',''); link = item.get('link','#'); snippet = item.get('snippet','').replace('\n',' ')
            texto_resultados += f"<div style='margin-bottom:10px;font-size:0.85em;'><a href='{link}' target='_blank' style='color:#1a0dab;text-decoration:none;font-weight:bold;'>{title}</a><p style='color:#545454;margin:2px 0;'>{snippet}</p><cite style='color:#006621;font-style:normal;font-size:0.9em;'>{link}</cite></div>\n"
        texto_resultados += "</div>"; logging.info(f"Búsqueda web OK: {len(resultados)} resultados.")
        return texto_resultados
    except requests.exceptions.Timeout: logging.error("Timeout búsqueda web."); return "<p><i>[Error: Timeout búsqueda web.]</i></p>"
    except requests.exceptions.RequestException as e: logging.error(f"Error búsqueda web: {e}"); return f"<p><i>[Error conexión búsqueda web.]</i></p>"
    except Exception as e: logging.error(f"Error inesperado búsqueda web: {e}"); return "<p><i>[Error inesperado búsqueda web.]</i></p>"


# --- Endpoints de la API ---

@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta): # Modelo actualizado
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    especializacion = datos.especializacion.lower()
    mensaje_usuario = datos.mensaje
    forzar_busqueda_web = datos.buscar_web
    current_user_id = datos.user_id # <-- Obtener user_id
    logging.info(f"Consulta: User={current_user_id}, Espec='{especializacion}', Web={forzar_busqueda_web}")

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""
    if current_user_id and DB_CONFIGURED:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cursor:
                    sql = "SELECT custom_prompt FROM user_settings WHERE user_id = %s"
                    cursor.execute(sql, (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'):
                        custom_prompt_text = result['custom_prompt'].strip()
                        if custom_prompt_text: logging.info(f"Prompt personalizado OK para user: {current_user_id}")
            except pymysql.MySQLError as e:
                logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                conn.close()
        else: logging.error(f"No se pudo conectar a BD para prompt.")

    # --- Construir Prompt del Sistema Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico]
    if custom_prompt_text:
        system_prompt_parts.append("\n\n### Instrucciones Adicionales del Usuario ###")
        system_prompt_parts.append(custom_prompt_text)
        logging.info("Prompt personalizado añadido.")
    system_prompt = "\n".join(system_prompt_parts)

    # --- Lógica OpenAI / Búsqueda Web ---
    texto_respuesta_final = ""
    # ... (resto de la lógica de OpenAI y búsqueda web igual que antes, usando `system_prompt`) ...
    try:
        logging.info("Llamada OpenAI 1...")
        respuesta_inicial = client.chat.completions.create(
            model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_usuario}],
            temperature=0.5, max_tokens=1500)
        texto_respuesta_inicial = respuesta_inicial.choices[0].message.content.strip(); logging.info("Respuesta OpenAI 1 OK.")
        activar_busqueda = forzar_busqueda_web or any(frase in texto_respuesta_inicial.lower() for frase in FRASES_BUSQUEDA)
        if activar_busqueda:
            web_resultados_html = buscar_google(mensaje_usuario)
            if "[Error" not in web_resultados_html:
                 logging.info("Llamada OpenAI 2 con contexto web...")
                 mensaje_con_contexto = f"Consulta: {mensaje_usuario}\nContexto web:\n{web_resultados_html}\n\nResponde consulta integrando contexto."
                 respuesta_con_contexto = client.chat.completions.create(
                     model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_con_contexto}],
                     temperature=0.5, max_tokens=1500)
                 texto_respuesta_final = respuesta_con_contexto.choices[0].message.content.strip(); logging.info("Respuesta OpenAI 2 OK.")
            else: texto_respuesta_final = texto_respuesta_inicial + "\n" + web_resultados_html
        else: texto_respuesta_final = texto_respuesta_inicial
    except APIError as e: raise HTTPException(status_code=503, detail=f"Error API OpenAI: {e.message}")
    except Exception as e: logging.error(f"Error /consulta: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Error interno.")

    return RespuestaConsulta(respuesta=texto_respuesta_final)


@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general"),
    user_id: int | None = Form(None) # <-- AÑADIDO user_id
):
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    filename = file.filename or "unknown"; content_type = file.content_type or ""; extension = filename.split('.')[-1].lower() if '.' in filename else ''
    current_user_id = user_id
    especializacion_lower = especializacion.lower()
    logging.info(f"Análisis: User={current_user_id}, File={filename}, Espec='{especializacion_lower}'")

    # --- Obtener Prompt Personalizado ---
    # (Misma lógica que en /consulta)
    custom_prompt_text = ""
    if current_user_id and DB_CONFIGURED:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cursor:
                    sql = "SELECT custom_prompt FROM user_settings WHERE user_id = %s"
                    cursor.execute(sql, (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'):
                        custom_prompt_text = result['custom_prompt'].strip()
                        if custom_prompt_text: logging.info(f"Prompt personalizado OK para user: {current_user_id}")
            except pymysql.MySQLError as e: logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally: conn.close()
        else: logging.error(f"No se pudo conectar a BD para prompt.")

    # --- Construir Prompt del Sistema Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico]
    if custom_prompt_text:
        system_prompt_parts.append("\n\n### Instrucciones Adicionales del Usuario ###")
        system_prompt_parts.append(custom_prompt_text)
        logging.info("Prompt personalizado añadido.")
    system_prompt = "\n".join(system_prompt_parts)

    # --- Lógica Procesamiento Archivo / Llamada OpenAI ---
    informe_html = ""; messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]
    try:
        # --- Caso Imagen ---
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando IMAGEN.")
            image_bytes = await file.read(); base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_prompt_image = ("Analiza la imagen, extrae su texto (OCR), y redacta un informe profesional HTML basado en ese texto. " "Sigue las instrucciones de formato HTML (h2, h3, p, ul, li, strong, em) y evita Markdown. " "Devuelve solo el HTML del informe.")
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": [ {"type": "text", "text": user_prompt_image}, {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}} ] } ]

        # --- Caso PDF/DOCX ---
        elif extension in ["pdf", "docx", "doc"]:
            logging.info(f"Procesando PDF/DOCX.")
            ruta_temporal = os.path.join(TEMP_DIR, f"up_{os.urandom(8).hex()}.{extension}")
            texto_extraido = ""; temp_file_saved = False
            try:
                with open(ruta_temporal, "wb") as buffer: shutil.copyfileobj(file.file, buffer); temp_file_saved = True
                texto_extraido = extraer_texto_pdf_docx(ruta_temporal, extension)
            finally:
                if temp_file_saved and os.path.exists(ruta_temporal): try: os.remove(ruta_temporal) except OSError as e: logging.error(f"Error borrar temp {ruta_temporal}: {e}")
            if texto_extraido.startswith("[Error"): raise ValueError(texto_extraido)
            if not texto_extraido: raise ValueError("No se extrajo texto del PDF/DOCX.")
            user_prompt_text = (f"Redacta un informe profesional HTML basado en el texto extraído:\n--- INICIO ---\n{texto_extraido}\n--- FIN ---\n" "Sigue formato HTML (h2, h3, p, ul, li, strong, em), evita Markdown. Devuelve solo HTML.")
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt_text} ]

        # --- Caso No Soportado ---
        else: raise HTTPException(status_code=415, detail=f"Tipo archivo no soportado: {content_type or extension}.")

        # --- Llamada OpenAI ---
        if not messages_payload: raise HTTPException(status_code=500, detail="Error interno: No se preparó payload IA.")
        logging.info(f"Llamada a OpenAI...")
        respuesta_informe = client.chat.completions.create( model="gpt-4-turbo", messages=messages_payload, temperature=0.3, max_tokens=2500 )
        informe_html = respuesta_informe.choices[0].message.content.strip(); logging.info(f"Informe generado OK.")
        if not informe_html.strip().startswith('<'): informe_html = f"<p>{informe_html}</p>" # Envolver si no es HTML

    except APIError as e: logging.error(f"Error API OpenAI /analizar: {e}"); raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except HTTPException as e: raise e
    except ValueError as e: logging.error(f"Error procesando {filename}: {e}"); raise HTTPException(status_code=400, detail=str(e))
    except Exception as e: logging.error(f"Error inesperado /analizar: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Error interno servidor.")
    finally: await file.close()

    return RespuestaAnalisis(informe=informe_html)

# --- Punto de Entrada (Opcional) ---
# if __name__ == "__main__": ...
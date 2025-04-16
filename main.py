# --- INICIO main.py v2.4.3-mt (Corrige NameError: Path not defined y restaura TODO el código) ---
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query, Path # Path AÑADIDO
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests
import base64
import logging
import psycopg2  # Driver PostgreSQL
import psycopg2.extras  # Para DictCursor
import tempfile
import re
import chardet  # Necesario para extraer_texto_simple
import hashlib
import httpx  # AÑADIDO para llamadas async a Google Places API
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False
# Helper para escapar HTML
from html import escape as htmlspecialchars
import time # Para posibles esperas en reintentos

# Configuración del Logging (preservando formato original)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - [%(funcName)s:%(lineno)d] - %(message)s',
    handlers=[
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Versión de la API (actualizada)
app = FastAPI(
    title="Asistente IA UBIKUA API v2.4.3-mt (Fix NameError Path + Completo)",
    version="2.4.3-mt",
    description="API para el Asistente IA UBIKUA con funcionalidades multi-tenant, RAG, y obtención de detalles de dirección."
)

# Configuración CORS (preservando original)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Ajustar en producción
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración (preservando estructura original + Añadida MAPS_API_ALL) ---
# Inicializar flags
client = None
SEARCH_CONFIGURED = False
MAPS_CONFIGURED = False
DB_CONFIGURED = False
PHP_BRIDGE_CONFIGURED = False

try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
         logger.warning("Var OPENAI_API_KEY no encontrada.")
    else:
        try:
            client = OpenAI(api_key=openai_api_key)
            logger.info("Cliente OpenAI OK.")
        except Exception as openai_err:
            logger.error(f"Error inicializando OpenAI: {openai_err}")
            client = None

    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Para Search
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logger.warning("Google API Keys (Search) no encontradas.")
    else:
        SEARCH_CONFIGURED = True
        logger.info("Google Search API Keys OK.")

    # --- NUEVA SECCIÓN: Clave API Google Maps/Places Backend ---
    MAPS_API_ALL = os.getenv("MAPS_API_ALL")
    if not MAPS_API_ALL:
        logger.warning("MAPS_API_ALL no encontrada. Endpoint detalles dirección deshabilitado.")
    else:
        MAPS_CONFIGURED = True
        logger.info("MAPS_API_ALL Key (Places Backend) OK.")
    # --- FIN NUEVA SECCIÓN ---

    DB_HOST = os.getenv("DB_HOST")
    DB_USER = os.getenv("DB_USER")
    DB_PASS = os.getenv("DB_PASS")
    DB_NAME = os.getenv("DB_NAME")
    DB_PORT_STR = os.getenv("DB_PORT", "5432")
    DB_PORT = 5432
    if DB_PORT_STR.isdigit():
        DB_PORT = int(DB_PORT_STR)
    else:
         logger.warning(f"DB_PORT ('{DB_PORT_STR}') inválido. Usando {DB_PORT}.")
    if not all([DB_HOST, DB_USER, DB_PASS, DB_NAME]):
        logger.warning("Faltan variables BD. Funcionalidad BD deshabilitada.")
    else:
        DB_CONFIGURED = True
        logger.info("Credenciales BD PostgreSQL OK.")

    PHP_FILE_SERVE_URL = os.getenv("PHP_FILE_SERVE_URL")
    PHP_API_SECRET_KEY = os.getenv("PHP_API_SECRET_KEY")
    if not PHP_FILE_SERVE_URL or not PHP_API_SECRET_KEY:
        logger.warning("Faltan PHP_FILE_SERVE_URL o PHP_API_SECRET_KEY. RAG deshabilitado.")
    else:
        PHP_BRIDGE_CONFIGURED = True
        logger.info("Config PHP Bridge OK.")

except Exception as e:
    logger.error(f"Error Configuración Crítica Inicial: {e}", exc_info=True)
    client = None; SEARCH_CONFIGURED = False; MAPS_CONFIGURED = False; DB_CONFIGURED = False; PHP_BRIDGE_CONFIGURED = False

# --- Modelos Pydantic (Preservando originales + Añadido PlaceDetailsResponse) ---
class PeticionConsulta(BaseModel):
    mensaje: str
    especializacion: str = "general"
    buscar_web: bool = False
    user_id: int | None = Field(None, description="ID del usuario que realiza la consulta")
    tenant_id: int | None = Field(None, description="ID del tenant/empresa del usuario")

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

class ProcessRequest(BaseModel): # Modelo para /process-document
    doc_id: int = Field(..., description="ID del documento en la BD")
    user_id: int = Field(..., description="ID del usuario propietario")
    tenant_id: int | None = Field(None, description="ID del tenant/empresa propietario")

class ProcessResponse(BaseModel):
    success: bool
    message: str | None = None
    error: str | None = None

# Modelo NUEVO para /direccion/detalles
class PlaceDetailsResponse(BaseModel):
    success: bool
    street_address: str | None = Field(None, description="Nombre de la calle y número")
    postal_code: str | None = Field(None, description="Código Postal")
    locality: str | None = Field(None, description="Localidad / Ciudad")
    province: str | None = Field(None, description="Provincia / Estado / Región")
    country: str | None = Field(None, description="País")
    error: str | None = Field(None, description="Mensaje de error si success es false")

# --- Prompts (COMPLETOS Y SIN CAMBIOS) ---
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de UBIKUA, un experto multidisciplinar que responde de manera "
    "clara, precisa y estéticamente impecable. Tus respuestas deben estar redactadas en HTML válido, "
    "usando etiquetas como <h2> y <h3> para encabezados, <p> para párrafos, <strong> para destacar información, "
    "y <ul> o <ol> para listas cuando sea necesario. Además, si la información lo requiere, utiliza etiquetas "
    "de tabla (<table>, <thead>, <tbody>, <tr>, <td>) para organizar datos de manera clara y estructurada. "
    "Debes evitar el uso de markdown o estilo plano. Asegúrate de que cada respuesta tenga una estructura lógica: "
    "comienza con un título principal, seguido de secciones bien delimitadas, tablas (si corresponde) y una "
    "conclusión clara. Siempre utiliza un tono profesional y adaptado al usuario, ofreciendo ejemplos y resúmenes "
    "que garanticen una comprensión total del contenido. Por favor, responde solo con HTML sin ningún comentario "
    "de código o markdown adicional."
)
BASE_PROMPT_ANALISIS_DOC = (
    "Eres el Asistente IA oficial de UBIKUA y un experto en redactar informes y análisis de documentos. "
    "A partir del texto suministrado, redacta un informe completo, profesional y estéticamente bien estructurado en HTML. "
    "Utiliza etiquetas HTML como <h1>, <h2> para títulos y subtítulos, <p> para párrafos, <strong> para resaltar puntos clave, "
    "y <table> con <thead>, <tbody>, <tr> y <td> para presentar datos o resúmenes numéricos. La respuesta debe incluir: "
    "1) un título principal, 2) secciones con encabezados relevantes, 3) listas o tablas cuando corresponda, y 4) una conclusión. "
    "Asegúrate de que la salida final sea un documento que se pueda copiar y pegar en Word o Google Docs sin perder el formato."
    "Por favor, responde solo con HTML sin ningún comentario de código o markdown adicional."
)
PROMPT_ESPECIALIZACIONES = {
    "general": "Ofrece una respuesta amplia, comprensiva y detallada, abarcando todos los puntos relevantes de la consulta.",
    "legal": "Adopta un enfoque riguroso y formal, utilizando terminología jurídica adecuada y estructurando la respuesta de forma clara y precisa.",
    "comunicacion": "Emplea un estilo persuasivo y creativo, con ejemplos y metáforas que faciliten la comprensión, y asegura que la respuesta sea atractiva y comunicativa.",
    "formacion": "Proporciona explicaciones didácticas y detalladas, estructuradas en secciones claramente delimitadas, con ejemplos prácticos y casos ilustrativos para facilitar el aprendizaje.",
    "informatica": "Ofrece respuestas técnicas precisas, explicando conceptos y procesos de tecnología de forma clara, con ejemplos, pseudocódigo o diagramas si es necesario.",
    "direccion": "Brinda una perspectiva estratégica, analizando tendencias y ofreciendo recomendaciones ejecutivas y bien fundamentadas, siempre con un tono profesional y asertivo.",
    "innovacion": "Responde de manera creativa e innovadora, proponiendo ideas disruptivas y soluciones fuera de lo convencional, utilizando analogías y ejemplos que inspiren nuevas perspectivas.",
    "contabilidad": "Proporciona respuestas precisas y estructuradas, con terminología contable adecuada, apoyadas en ejemplos numéricos y análisis detallados cuando sea relevante.",
    "administracion": "Enfócate en la eficiencia y organización, ofreciendo análisis claros sobre procesos, recomendaciones prácticas y estructuradas que faciliten la gestión y toma de decisiones."
}
FRASES_BUSQUEDA = ["no tengo información", "no dispongo de información", "no tengo acceso", "no sé"]

# --- Temp Dir (Igual) ---
TEMP_DIR = "/tmp/ubikua_uploads"
try:
    os.makedirs(TEMP_DIR, exist_ok=True)
    logger.info(f"Directorio temporal OK: {TEMP_DIR}")
except OSError as e:
    logger.error(f"Error crear dir temporal {TEMP_DIR}: {e}.")

# --- Funciones Auxiliares (Preservando originales con leves mejoras de logging/errores) ---

def get_db_connection():
    """Establece y devuelve una conexión a la base de datos PostgreSQL."""
    if not DB_CONFIGURED: logger.error("get_db_connection: DB no config."); return None
    try:
        conn = psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT, connect_timeout=5)
        return conn
    except psycopg2.OperationalError as op_err: logger.error(f"Error Op BD Conexión: {op_err}", exc_info=False); return None
    except Exception as error: logger.error(f"Error Gral BD Conexión: {error}", exc_info=True); return None

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    """Extrae texto de archivos PDF, DOC y DOCX."""
    # (Código interno completo preservado de v2.4.1)
    texto = ""; filename_for_log = os.path.basename(ruta_archivo); logger.info(f"Extrayendo ({extension.upper()}) de: {filename_for_log}")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                try:
                    lector = PdfReader(archivo, strict=False); num_paginas = len(lector.pages); logger.info(f"Procesando {num_paginas} pág PDF.")
                    if lector.is_encrypted: logger.warning(f"PDF '{filename_for_log}' encriptado.")
                    for i, pagina in enumerate(lector.pages):
                        try: texto_pagina = pagina.extract_text();
                            if texto_pagina: texto += texto_pagina + "\n"
                        except Exception as page_error: logger.warning(f"Error extraer pág {i+1} {filename_for_log}: {page_error}")
                except pdf_errors.PdfReadError as pdf_err: logger.error(f"Error PyPDF2 leer {filename_for_log}: {pdf_err}"); return "[Error PDF: Dañado/No Soportado]"
        elif extension in ["doc", "docx"]:
             try: doc = Document(ruta_archivo); parrafos = [p.text for p in doc.paragraphs if p.text and p.text.strip()]; texto = "\n".join(parrafos)
             except PackageNotFoundError: logger.error(f"Error DOCX '{filename_for_log}': Inválido."); return "[Error DOCX: Inválido]"
             except Exception as docx_error: logger.error(f"Error DOCX '{filename_for_log}': {docx_error}", exc_info=True); return "[Error interno DOCX]"
        else: logger.error(f"Tipo archivo '{extension}' no esperado extraer_pdf_docx"); return "[Error: Tipo no esperado]"
        texto_limpio = texto.strip();
        if not texto_limpio: logger.warning(f"Texto útil vacío: '{filename_for_log}'"); return "[Archivo sin texto extraíble]"
        logger.info(f"Texto {extension.upper()} OK ({len(texto_limpio)} chars) '{filename_for_log}'.")
        return texto_limpio
    except FileNotFoundError: logger.error(f"FNF Sistema: '{filename_for_log}'."); return "[Error: Archivo no encontrado]"
    except Exception as e: logger.error(f"Error Gral extraer {extension.upper()} '{filename_for_log}': {e}", exc_info=True); return f"[Error interno {extension.upper()}]"


def extraer_texto_simple(ruta_archivo: str) -> str:
    """Extrae texto de archivos planos (TXT, CSV) detectando encoding."""
    # (Código interno completo preservado de v2.4.1)
    filename_for_log = os.path.basename(ruta_archivo); logger.info(f"Extrayendo simple: {filename_for_log}"); texto = ""; detected_encoding = 'utf-8'
    try:
        with open(ruta_archivo, 'rb') as fb:
            raw_data = fb.read();
            if not raw_data: logger.warning(f"Archivo vacío: {filename_for_log}"); return ""
            detection = chardet.detect(raw_data); confidence = detection.get('confidence', 0); encoding = detection.get('encoding')
            if encoding and confidence > 0.6: detected_encoding = encoding; logger.info(f"Encoding: {detected_encoding} (Conf: {confidence:.2f}) '{filename_for_log}'")
            else: logger.info(f"Encoding incierto ({detection}), usando '{detected_encoding}' '{filename_for_log}'.")
        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f: texto = f.read()
        texto_limpio = texto.strip();
        if not texto_limpio: logger.warning(f"Texto útil vacío: '{filename_for_log}' (simple)"); return "[Archivo sin texto extraíble]"
        logger.info(f"Texto simple OK ({len(texto_limpio)} chars) '{filename_for_log}'.")
        return texto_limpio
    except FileNotFoundError: logger.error(f"FNF: '{filename_for_log}' simple."); return "[Error: Archivo no encontrado]"
    except UnicodeDecodeError as ude:
         logger.error(f"Error decodif '{filename_for_log}' con '{detected_encoding}': {ude}")
         try:
             logger.info(f"Reintento '{filename_for_log}' con ISO-8859-1...");
             with open(ruta_archivo, 'r', encoding='iso-8859-1', errors='ignore') as f_fallback: texto = f_fallback.read()
             texto_limpio = texto.strip();
             if not texto_limpio: return "[Archivo sin texto extraíble]"
             logger.info(f"Texto OK fallback ISO-8859-1 ({len(texto_limpio)} chars).")
             return texto_limpio
         except Exception as e_fallback: logger.error(f"Fallo fallback '{filename_for_log}': {e_fallback}"); return "[Error Codificación]"
    except Exception as e: logger.error(f"Error inesperado simple '{filename_for_log}': {e}", exc_info=True); return "[Error interno texto plano]"


def buscar_google(query: str) -> str:
    """Realiza búsqueda en Google Custom Search, devuelve HTML formateado."""
    # (Código interno completo preservado de v2.4.1)
    if not SEARCH_CONFIGURED: logger.warning("buscar_google sin config."); return "<p><i>[Búsqueda web no config.]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"; params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3, "lr": "lang_es"}
    logger.info(f"Buscando Google Search: '{query}'")
    try:
        response = requests.get(url, params=params, timeout=10); response.raise_for_status(); data = response.json()
        if "error" in data: error_details = data["error"].get("message", "?"); logger.error(f"Error Google Search API: {error_details}"); return f"<p><i>[Error búsqueda: {htmlspecialchars(error_details)}]</i></p>"
        resultados = data.get("items", [])
        if not resultados: logger.info("Búsqueda web sin resultados."); return "<p><i>[No resultados web.]</i></p>"
        texto_resultados = "<div class='google-results' style='margin-top:15px; padding-top:10px; border-top: 1px solid #eee;'><h4 style='font-size:0.9em;color:#555; margin-bottom: 5px;'>Resultados web relacionados:</h4><ul>"
        for item in resultados:
            title = item.get('title','?'); link = item.get('link','#'); snippet = item.get('snippet','')
            if snippet: snippet = re.sub('<.*?>', '', snippet).replace('\n',' ').strip(); else: snippet = "No descripción."
            texto_resultados += (f"<li style='margin-bottom: 10px; padding-left: 5px; border-left: 3px solid #ddd;'><a href='{link}' target='_blank' style='font-weight: bold; color: #1a0dab; text-decoration: none; display: block; margin-bottom: 2px;'>{htmlspecialchars(title)}</a><p style='font-size: 0.85em; margin: 0; color: #333;'>{htmlspecialchars(snippet)}</p><cite style='font-size: 0.8em; color: #006621; display: block; margin-top: 2px;'>{htmlspecialchars(link)}</cite></li>")
        texto_resultados += "</ul></div>"; logger.info(f"Búsqueda web OK: {len(resultados)} resultados."); return texto_resultados
    except requests.exceptions.Timeout: logger.error("Timeout búsqueda web."); return "<p><i>[Error: Timeout búsqueda web.]</i></p>"
    except requests.exceptions.RequestException as e: logger.error(f"Error conexión búsqueda web: {e}"); return "<p><i>[Error conexión búsqueda web.]</i></p>"
    except Exception as e: logger.error(f"Error inesperado búsqueda web: {e}", exc_info=True); return "<p><i>[Error inesperado búsqueda web.]</i></p>"


# --- Endpoint /process-document (Preservando formato/comentarios v2.3.7-mt + mejoras v2.4.x) ---
@app.post("/process-document", response_model=ProcessResponse)
async def process_document_text(request: ProcessRequest):
    """
    Procesa un documento previamente subido: obtiene su contenido (vía PHP bridge),
    extrae el texto y lo guarda en la base de datos junto con la marca 'procesado'.
    También actualiza el vector FTS (manejado por trigger en BD).
    """
    doc_id = request.doc_id
    current_user_id = request.user_id
    current_tenant_id = request.tenant_id # OBTENIDO tenant_id

    # Validar que tenemos los IDs necesarios
    if not isinstance(current_user_id, int) or not isinstance(current_tenant_id, int):
        logger.error(f"IDs inválidos recibidos en /process-document: User='{current_user_id}', Tenant='{current_tenant_id}' para Doc ID {doc_id}")
        return ProcessResponse(success=False, error="User ID y Tenant ID deben ser números enteros válidos.")

    logger.info(f"Procesar doc ID: {doc_id} user: {current_user_id} tenant: {current_tenant_id}")

    if not DB_CONFIGURED or not PHP_BRIDGE_CONFIGURED:
        error_msg = "Configuración incompleta en el backend para procesar documentos."
        if not DB_CONFIGURED: error_msg += " (Falta config BD)"
        if not PHP_BRIDGE_CONFIGURED: error_msg += " (Falta config PHP Bridge)"
        logger.error(error_msg + f" - Solicitud para doc {doc_id}")
        return ProcessResponse(success=False, error=error_msg)

    conn = None; original_fname = None; temp_path = None
    try:
        # 1. Obtener info del documento (Filtrando por tenant_id)
        conn = get_db_connection()
        if not conn: raise ConnectionError("No se pudo conectar a BD (info doc).") # Lanzar error específico

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            sql_select = """
                SELECT original_filename, file_type, stored_path, procesado
                FROM user_documents
                WHERE id = %s AND user_id = %s AND tenant_id = %s
            """
            cursor.execute(sql_select, (doc_id, current_user_id, current_tenant_id))
            doc_info = cursor.fetchone()
            if not doc_info:
                 logger.warning(f"Doc {doc_id} no encontrado para U={current_user_id}/T={current_tenant_id}.")
                 raise FileNotFoundError(f"Documento ID {doc_id} no encontrado para este usuario/tenant.")

            original_fname = doc_info['original_filename']
            file_type = doc_info['file_type']
            stored_path = doc_info['stored_path'] # No usado si usamos PHP Bridge
            is_already_processed = doc_info['procesado']

            if is_already_processed:
                 logger.info(f"Doc {doc_id} ('{original_fname}') ya procesado. Omitiendo.")
                 return ProcessResponse(success=True, message="El documento ya estaba procesado.")

        conn.close(); conn = None # Cerrar conexión BD tras obtener info

        # 2. Obtener contenido vía PHP Bridge
        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&tenant_id={current_tenant_id}&api_key={PHP_API_SECRET_KEY}"
        logger.info(f"Solicitando doc {doc_id} a PHP: {serve_url}")
        response = requests.get(serve_url, timeout=120, stream=True) # Aumentar timeout
        response.raise_for_status()
        logger.info(f"Respuesta PHP Bridge OK (Status: {response.status_code}).")

        # 3. Guardar temporal y extraer texto
        file_ext = os.path.splitext(original_fname)[1].lower().strip('.') if original_fname else ''
        extracted_text = None
        TEXT_EXTENSIONS_PROC = ["pdf", "doc", "docx", "txt", "csv"]

        if file_ext in TEXT_EXTENSIONS_PROC:
            with tempfile.NamedTemporaryFile(mode='wb', suffix=f'.{file_ext}', dir=TEMP_DIR, delete=False) as temp_file:
                temp_path = temp_file.name; bytes_written = 0
                try:
                    logger.info(f"Guardando temporal: {temp_path}")
                    for chunk in response.iter_content(chunk_size=1024*1024): temp_file.write(chunk); bytes_written += len(chunk)
                    logger.info(f"{bytes_written} bytes escritos a {temp_path}")
                    if bytes_written == 0: extracted_text = "[Archivo vacío recibido]"
                except Exception as write_err: logger.error(f"Error escribir temporal {temp_path}: {write_err}", exc_info=True); raise IOError(f"Fallo escribir temporal {original_fname}")

            if extracted_text is None: # Si no estaba vacío
                 logger.info(f"Extrayendo texto de {temp_path}...")
                 if file_ext in ['pdf', 'doc', 'docx']: extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
                 else: extracted_text = extraer_texto_simple(temp_path)
        else: logger.warning(f"Extracción no soportada ext '{file_ext}' (Doc: {doc_id})"); extracted_text = f"[Extracción no soportada tipo: {file_ext}]"

        # Borrar temporal ahora
        if temp_path and os.path.exists(temp_path):
            try: os.remove(temp_path); logger.info(f"Temporal {temp_path} eliminado."); temp_path = None
            except OSError as e: logger.error(f"Error borrar temporal {temp_path}: {e}")

        # Validar texto antes de BD
        if extracted_text is None or not extracted_text.strip() or extracted_text.strip().startswith("[Error"):
             if extracted_text is None: extracted_text = "[Error: Extracción None]"
             if not extracted_text.strip(): extracted_text = "[Archivo vacío o sin texto]"
             logger.error(f"Extracción fallida/vacía doc {doc_id}. Texto BD: '{extracted_text[:100]}...'")

        # 4. Actualizar BD
        logger.info(f"Actualizando BD doc {doc_id} / T={current_tenant_id}..."); conn = get_db_connection()
        if not conn: raise ConnectionError("Fallo reconexión BD (guardar texto).")
        with conn.cursor() as cursor:
            sql_update = "UPDATE user_documents SET extracted_text = %s, procesado = TRUE WHERE id = %s AND user_id = %s AND tenant_id = %s"
            MAX_LEN = 15 * 1024 * 1024; text_save = extracted_text[:MAX_LEN] if len(extracted_text) > MAX_LEN else extracted_text
            if len(extracted_text) > MAX_LEN: logger.warning(f"Texto truncado {MAX_LEN} chars BD (doc {doc_id}). Orig: {len(extracted_text)}")
            cursor.execute(sql_update, (text_save, doc_id, current_user_id, current_tenant_id)); rows = cursor.rowcount; conn.commit()
            if rows == 0: logger.warning(f"UPDATE procesado no afectó filas doc {doc_id}/T={current_tenant_id}.")
            else: logger.info(f"BD actualizada OK doc {doc_id} ({rows} fila).")
        return ProcessResponse(success=True, message="Documento procesado.")

    # --- Manejo Excepciones /process-document ---
    except FileNotFoundError as e: logger.error(f"FNF procesando doc {doc_id}: {e}"); return ProcessResponse(success=False, error=str(e))
    except ConnectionError as e: return ProcessResponse(success=False, error=f"Error conexión BD: {e}")
    except requests.exceptions.RequestException as e: logger.error(f"Error PHP Bridge doc {doc_id}: {e}", exc_info=True); status = e.response.status_code if e.response is not None else 'N/A'; return ProcessResponse(success=False, error=f"Error obtener archivo ({status}).")
    except IOError as e: logger.error(f"Error I/O temporal doc {doc_id}: {e}", exc_info=True); return ProcessResponse(success=False, error=f"Error archivo temporal: {e}")
    except psycopg2.Error as db_err: logger.error(f"Error BD procesando doc {doc_id}: {db_err}", exc_info=True);
        if conn and not conn.closed: conn.rollback();
        return ProcessResponse(success=False, error="Error BD durante procesamiento.")
    except Exception as e: logger.error(f"Error general procesando doc {doc_id}: {e}", exc_info=True); return ProcessResponse(success=False, error=f"Error interno servidor ({type(e).__name__}).")
    finally:
        if temp_path and os.path.exists(temp_path): try: os.remove(temp_path); logger.info(f"Temporal {temp_path} eliminado en finally.")
        except OSError as e: logger.error(f"Error borrar temp {temp_path} en finally: {e}")
        if conn and not conn.closed: conn.close()


# --- Endpoint de consulta (/consulta - Preservando formato/comentarios v2.3.7-mt + mejoras v2.4.x) ---
@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    """
    Procesa consulta: construye prompt (base+esp+mem+RAG), llama OpenAI, busca web, guarda historial.
    (Preserva estructura y comentarios originales)
    """
    if not client: logger.error("Llamada /consulta sin cliente OpenAI."); raise HTTPException(503, "Servicio IA no disponible.")
    current_user_id = datos.user_id; current_tenant_id = datos.tenant_id
    if not isinstance(current_user_id, int) or not isinstance(current_tenant_id, int): logger.error(f"IDs inválidos /consulta: U={current_user_id}, T={current_tenant_id}"); raise HTTPException(400, "User/Tenant ID inválidos.")
    especializacion = datos.especializacion.lower() if datos.especializacion else "general"; mensaje_usuario = datos.mensaje.strip(); forzar_busqueda_web = datos.buscar_web
    logger.info(f"Consulta: U={current_user_id},T={current_tenant_id},E='{especializacion}',Web={forzar_busqueda_web},Msg='{mensaje_usuario[:100]}...'")
    if not mensaje_usuario: return RespuestaConsulta(respuesta="<p>Por favor, introduce tu consulta.</p>")

    # Obtener prompt personalizado (Memoria)
    custom_prompt_text = ""
    if DB_CONFIGURED:
        conn_prompt = get_db_connection()
        if conn_prompt:
            try:
                with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    sql_prompt = "SELECT custom_prompt FROM user_settings WHERE user_id = %s AND tenant_id = %s"
                    cursor.execute(sql_prompt, (current_user_id, current_tenant_id))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt') and result['custom_prompt'].strip(): custom_prompt_text = result['custom_prompt'].strip(); logger.info(f"Memoria OK U={current_user_id}/T={current_tenant_id}.")
                    else: logger.info(f"No Memoria U={current_user_id}/T={current_tenant_id}.")
            except (Exception, psycopg2.Error) as e: logger.error(f"Error BD get Memoria U={current_user_id}: {e}", exc_info=True)
            finally: conn_prompt.close()
        else: logger.warning(f"No conexión BD Memoria U={current_user_id}")

    # Obtener contexto documental (RAG)
    document_context = ""; MAX_RAG_TOKENS = 3500
    if DB_CONFIGURED:
        conn_docs = get_db_connection()
        if conn_docs:
            try:
                with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    search_query_cleaned = re.sub(r'[!\'()|&:*<>~@]', ' ', mensaje_usuario).strip(); search_query_terms = search_query_cleaned.split()
                    if not search_query_terms: logger.info(f"Msg RAG vacío tras limpiar.")
                    else:
                        fts_query_string = ' & '.join(search_query_terms); logger.info(f"Buscando RAG FTS: '{fts_query_string}' U={current_user_id}/T={current_tenant_id}")
                        sql_fts = "SELECT original_filename, extracted_text, ts_rank_cd(fts_vector, plainto_tsquery('spanish', %(query)s)) as relevance FROM user_documents WHERE user_id = %(user_id)s AND tenant_id = %(tenant_id)s AND is_active_for_ai = TRUE AND procesado = TRUE AND fts_vector @@ plainto_tsquery('spanish', %(query)s) AND extracted_text IS NOT NULL AND extracted_text != '' AND NOT extracted_text LIKE '[Error%%' AND NOT extracted_text LIKE '[Archivo vacío%%' ORDER BY relevance DESC LIMIT 5"
                        cursor.execute(sql_fts, {'query': fts_query_string, 'user_id': current_user_id, 'tenant_id': current_tenant_id})
                        relevant_docs = cursor.fetchall()
                        if relevant_docs:
                            logger.info(f"Encontrados {len(relevant_docs)} docs RAG pots."); context_parts = ["\n\n### Contexto de tus Documentos ###\n(Información extraída de tus archivos para ayudar a responder tu consulta)\n"]; current_token_count = 0; docs_included_count = 0; MIN_PARTIAL_TOKENS = 150
                            for doc in relevant_docs:
                                filename = doc['original_filename']; text = doc['extracted_text']; relevance_score = doc['relevance']; logger.debug(f"Eval RAG: '{filename}' (Rel: {relevance_score:.4f})")
                                doc_tokens_estimated = len(text.split()) * 1.3
                                if current_token_count + doc_tokens_estimated <= MAX_RAG_TOKENS: context_parts.append(f"\n--- Documento: {htmlspecialchars(filename)} (Relevancia: {relevance_score:.2f}) ---"); context_parts.append(text); current_token_count += doc_tokens_estimated; docs_included_count += 1; logger.debug(f"Add RAG: '{filename}'. Toks: ~{current_token_count:.0f}/{MAX_RAG_TOKENS}");
                                if current_token_count >= MAX_RAG_TOKENS: logger.warning(f"Límite RAG ({MAX_RAG_TOKENS}) {docs_included_count} docs."); break
                                else:
                                    remaining_tokens = MAX_RAG_TOKENS - current_token_count
                                    if remaining_tokens > MIN_PARTIAL_TOKENS: available_chars = max(100, int(remaining_tokens / 1.3)); partial_text = text[:available_chars]; context_parts.append(f"\n--- Documento (Parcial): {htmlspecialchars(filename)} (Relevancia: {relevance_score:.2f}) ---"); context_parts.append(partial_text + "..."); current_token_count += remaining_tokens; docs_included_count += 1; logger.warning(f"Incluida porción RAG '{filename}'. Límite RAG.")
                                    else: logger.info(f"Doc RAG '{filename}' omitido (límite tokens)."); break
                            if docs_included_count > 0: document_context = "\n".join(context_parts); logger.info(f"Contexto RAG: {docs_included_count} docs (~{current_token_count:.0f} tokens).")
                            else: logger.info("Ningún doc RAG añadido a contexto.")
                        else: logger.info(f"No docs RAG encontrados.")
            except (Exception, psycopg2.Error) as e: logger.error(f"Error BD RAG U={current_user_id}: {e}", exc_info=True); document_context = "\n<p><i>[Error buscar docs.]</i></p>"
            finally:
                if conn_docs: conn_docs.close()
        else: logger.warning(f"No conexión BD RAG U={current_user_id}")

    # Combinar prompts
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"]); system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico];
    if custom_prompt_text: system_prompt_parts.append(f"\n### Instrucciones Adicionales (Memoria) ###\n{custom_prompt_text}")
    if document_context: system_prompt_parts.append(document_context)
    system_prompt = "\n".join(filter(None, system_prompt_parts))
    # logger.debug(f"System Prompt Final:\n{system_prompt}")

    # Llamada a OpenAI con reintentos
    texto_respuesta_final = "<p><i>Error generando respuesta.</i></p>"; MAX_RETRIES_OPENAI = 2
    for attempt in range(MAX_RETRIES_OPENAI):
         try:
            logger.info(f"Llamando OpenAI (Intento {attempt + 1}) U={current_user_id}..."); respuesta_inicial = client.chat.completions.create(model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_usuario}], temperature=0.6, max_tokens=2000 )
            if not respuesta_inicial.choices or not respuesta_inicial.choices[0].message or not respuesta_inicial.choices[0].message.content: logger.error("Respuesta OpenAI inválida."); texto_respuesta_final = "<p><i>Error: Respuesta IA inválida.</i></p>"; continue
            texto_respuesta_final = respuesta_inicial.choices[0].message.content.strip(); finish_reason = respuesta_inicial.choices[0].finish_reason; logger.info(f"Respuesta OpenAI OK (Len: {len(texto_respuesta_final)}, Fin: {finish_reason}).")
            if finish_reason == 'length': logger.warning(f"Respuesta OpenAI truncada."); texto_respuesta_final += "\n<p><i>(Respuesta incompleta...)</i></p>"
            necesita_web = any(frase in texto_respuesta_final.lower() for frase in FRASES_BUSQUEDA) or forzar_busqueda_web
            if necesita_web: logger.info(f"Requiere búsqueda web (Forzado: {forzar_busqueda_web})..."); web_resultados_html = buscar_google(mensaje_usuario);
                if web_resultados_html and not web_resultados_html.startswith("<p><i>["): texto_respuesta_final += "\n\n" + web_resultados_html; logger.info("Resultados web añadidos.")
                else: logger.info("Búsqueda web sin resultados/error.")
            else: logger.info("No requiere búsqueda web.")
            break # Éxito
         except APIError as e: logger.error(f"Error API OpenAI /consulta (Intento {attempt + 1}): {e}", exc_info=True); texto_respuesta_final = f"<p><i>Error IA: {e.message}.</i></p>"; time.sleep(0.5)
         except Exception as e: logger.error(f"Error /consulta (Intento {attempt + 1}): {e}", exc_info=True); texto_respuesta_final = "<p><i>Error interno consulta.</i></p>"; time.sleep(0.5)

    # Guardar en historial
    if DB_CONFIGURED:
        conn_hist = get_db_connection();
        if conn_hist:
            try:
                with conn_hist.cursor() as cursor: cursor.execute("INSERT INTO historial (usuario_id, tenant_id, pregunta, respuesta, fecha_hora) VALUES (%s, %s, %s, %s, NOW())", (current_user_id, current_tenant_id, mensaje_usuario, texto_respuesta_final)); conn_hist.commit(); logger.info(f"Consulta guardada historial U={current_user_id}/T={current_tenant_id}.")
            except (Exception, psycopg2.Error) as e_hist: logger.error(f"Error guardar historial U={current_user_id}: {e_hist}", exc_info=True); conn_hist.rollback()
            finally: conn_hist.close()
        else: logger.warning("No se guardó historial (sin conexión BD).")

    return RespuestaConsulta(respuesta=texto_respuesta_final)


# --- Endpoint /analizar-documento (Preservando formato/comentarios v2.3.7-mt + mejoras v2.4.x) ---
@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general"),
    user_id: int | None = Form(None),
    tenant_id: int | None = Form(None)
):
    """Analiza documento (imagen o texto), devuelve informe HTML."""
    # (Código interno completo preservado de v2.4.1)
    if not client: logger.error("Llamada /analizar sin cliente OpenAI."); raise HTTPException(503, "Servicio IA no disponible.")
    current_user_id = user_id; current_tenant_id = tenant_id
    if not isinstance(current_user_id, int) or not isinstance(current_tenant_id, int): logger.error(f"IDs inválidos /analizar: U='{current_user_id}', T='{current_tenant_id}'"); raise HTTPException(400, "User/Tenant ID inválidos.")
    filename = file.filename if file.filename else "archivo_subido"; content_type = file.content_type or ""; base, dot, extension = filename.rpartition('.'); extension = extension.lower() if dot else ''
    especializacion_lower = especializacion.lower() if especializacion else "general"; logger.info(f"Análisis Doc: U={current_user_id}, T={current_tenant_id}, File='{filename}', Type='{content_type}', Espec='{especializacion_lower}'")
    custom_prompt_text = "" # Obtener Memoria
    if DB_CONFIGURED: conn_prompt = get_db_connection();
    if conn_prompt: try: with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor: cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s AND tenant_id = %s", (current_user_id, current_tenant_id)); result = cursor.fetchone(); if result and result.get('custom_prompt') and result['custom_prompt'].strip(): custom_prompt_text = result['custom_prompt'].strip(); logger.info(f"Memoria OK análisis U={current_user_id}.")
    except (Exception, psycopg2.Error) as e_prompt: logger.error(f"Error BD get Memoria análisis U={current_user_id}: {e_prompt}", exc_info=True)
    finally: conn_prompt.close()
    else: logger.warning(f"No conexión BD Memoria análisis.")
    # Construir Prompt Análisis
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"]); system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico];
    if custom_prompt_text: system_prompt_parts.append(f"\n### Memoria ###\n{custom_prompt_text}"); system_prompt = "\n".join(filter(None, system_prompt_parts))
    messages_payload = []; IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]; TEXT_EXTENSIONS = ["pdf", "doc", "docx", "txt", "csv"]; temp_filename_analisis = None
    try:
        if content_type in IMAGE_MIMES: # Imagen
            logger.info(f"Procesando imagen '{filename}' análisis Vision."); image_bytes = await file.read(); MAX_IMAGE_SIZE = 20*1024*1024
            if len(image_bytes) > MAX_IMAGE_SIZE: logger.error(f"Imagen '{filename}' excede {MAX_IMAGE_SIZE / (1024*1024):.1f} MB."); raise HTTPException(413, f"Imagen excede {MAX_IMAGE_SIZE / (1024*1024):.1f} MB.")
            base64_image = base64.b64encode(image_bytes).decode('utf-8'); user_prompt = "Analiza imagen y genera informe HTML."
            messages_payload = [{"role": "system", "content": system_prompt}, {"role": "user", "content": [{"type": "text", "text": user_prompt}, {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}}]}]
        elif extension in TEXT_EXTENSIONS: # Texto
            logger.info(f"Procesando texto '{filename}' análisis."); texto_extraido = "";
            with tempfile.NamedTemporaryFile(mode='wb', suffix=f'.{extension}', dir=TEMP_DIR, delete=False) as temp_file: temp_filename_analisis = temp_file.name; # ... [código guardar temporal igual] ...
                 try: while True: chunk = await file.read(8192); if not chunk: break; temp_file.write(chunk); logger.info(f"'{filename}' -> temp '{temp_filename_analisis}'")
                 except Exception as copy_err: logger.error(f"Error copiar a temp '{temp_filename_analisis}': {copy_err}", exc_info=True); raise HTTPException(500, "Error guardar temporal.")
            try: # ... [código extraer texto igual] ...
                 if extension in ['pdf', 'doc', 'docx']: texto_extraido = extraer_texto_pdf_docx(temp_filename_analisis, extension)
                 else: texto_extraido = extraer_texto_simple(temp_filename_analisis)
            finally: if temp_filename_analisis and os.path.exists(temp_filename_analisis): try: os.remove(temp_filename_analisis); logger.info(f"Temp '{temp_filename_analisis}' eliminado."); temp_filename_analisis = None; except OSError as e: logger.error(f"Error borrar temp '{temp_filename_analisis}': {e}")
            # Validar y Truncar texto
            if texto_extraido.startswith("[Error") or not texto_extraido.strip(): error_msg = texto_extraido if texto_extraido.startswith("[Error") else "[Archivo vacío]"; logger.error(f"Error/vacío extracción '{filename}': {error_msg}"); raise HTTPException(400, f"Error extraer texto: {error_msg}")
            MAX_ANALYSIS_TOKENS = 100000; estimated_tokens = len(texto_extraido.split()) * 1.3;
            if estimated_tokens > MAX_ANALYSIS_TOKENS: logger.warning(f"Texto '{filename}' truncado (~{MAX_ANALYSIS_TOKENS} tokens)."); max_chars = int(MAX_ANALYSIS_TOKENS / 1.3); texto_extraido = texto_extraido[:max_chars] + "\n[TRUNCADO]"
            user_prompt = f"Redacta informe HTML basado en texto de '{htmlspecialchars(filename)}':\n--- INICIO ---\n{texto_extraido}\n--- FIN ---"; messages_payload = [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]
        else: logger.error(f"Tipo archivo no soportado análisis: '{content_type or extension}' ('{filename}')"); raise HTTPException(415, f"Tipo archivo '{content_type or extension}' no soportado.")
        if not messages_payload: logger.critical("Payload OpenAI no generado /analizar."); raise HTTPException(500, "Error interno preparando IA.")
        # Llamar OpenAI
        informe_html = "<p><i>Error generando informe.</i></p>"; MAX_RETRIES_OPENAI = 2
        for attempt in range(MAX_RETRIES_OPENAI): # ... [código llamada OpenAI igual] ...
             try: logger.info(f"Llamando OpenAI análisis '{filename}' (Intento {attempt + 1})..."); respuesta_informe = client.chat.completions.create(model="gpt-4-turbo", messages=messages_payload, temperature=0.4, max_tokens=3000 );
                 if not respuesta_informe.choices or not respuesta_informe.choices[0].message or not respuesta_informe.choices[0].message.content: logger.error(f"Respuesta OpenAI inválida análisis '{filename}'."); continue
                 informe_html = respuesta_informe.choices[0].message.content.strip(); finish_reason = respuesta_informe.choices[0].finish_reason; logger.info(f"Informe OK '{filename}' (Len: {len(informe_html)}, Fin: {finish_reason}).")
                 if finish_reason == 'length': logger.warning(f"Informe OpenAI truncado '{filename}'."); informe_html += "\n<p><i>(Informe incompleto...)</i></p>"
                 break
             except APIError as e: logger.error(f"Error API OpenAI /analizar '{filename}' (Intento {attempt + 1}): {e}", exc_info=True); time.sleep(0.5)
             except Exception as e: logger.error(f"Error OpenAI /analizar '{filename}' (Intento {attempt + 1}): {e}", exc_info=True); time.sleep(0.5)
             if attempt == MAX_RETRIES_OPENAI - 1: raise HTTPException(503, f"Error OpenAI al analizar tras {MAX_RETRIES_OPENAI} intentos.")
        # Limpieza HTML
        if BS4_AVAILABLE: try: # ... (código limpieza BS4 igual) ...
            if "<!DOCTYPE html>" in informe_html or "<html" in informe_html: soup = BeautifulSoup(informe_html, 'html.parser');
            if soup.body: informe_html = soup.body.decode_contents(); logger.info("Limpieza HTML: body extraído.")
            elif soup.html: informe_html = soup.html.decode_contents(); logger.info("Limpieza HTML: contenido html extraído.")
            informe_html = re.sub(r'^```[a-zA-Z]*\s*', '', informe_html, flags=re.IGNORECASE).strip(); informe_html = re.sub(r'\s*```$', '', informe_html).strip()
        except Exception as e_bs4: logger.error(f"Error limpieza HTML informe: {e_bs4}")
        if not re.search(r'<[a-z][\s\S]*>', informe_html, re.IGNORECASE): logger.warning("Informe OpenAI no parece HTML."); informe_html = f"<p>{htmlspecialchars(informe_html)}</p>"
        return RespuestaAnalisis(informe=informe_html)
    except HTTPException as e: raise e
    except Exception as e:
        logger.error(f"Error general /analizar '{filename}': {e}", exc_info=True)
        if temp_filename_analisis and os.path.exists(temp_filename_analisis): try: os.remove(temp_filename_analisis); logger.info(f"Temp '{temp_filename_analisis}' eliminado catch general.")
        except OSError as e_final_remove: logger.error(f"Error borrar temp '{temp_filename_analisis}' catch: {e_final_remove}")
        raise HTTPException(status_code=500, detail=f"Error interno procesando archivo ({type(e).__name__}).")
    finally: await file.close()


# --- Endpoint /direccion/detalles/{place_id} (Preservando formato/comentarios v2.4.1) ---
@app.get("/direccion/detalles/{place_id}", response_model=PlaceDetailsResponse)
async def obtener_detalles_direccion(
    place_id: str = Path(..., description="ID del lugar obtenido de Google Places Autocomplete"),
    user_id: int | None = Query(None, description="ID del usuario (opcional para logging)"),
    tenant_id: int | None = Query(None, description="ID del tenant (opcional para logging)")
):
    """
    Obtiene detalles de dirección (calle, CP, localidad, provincia, país)
    a partir de un Place ID. Usa API Key segura del backend (MAPS_API_ALL).
    """
    logger.info(f"Solicitud detalles dirección Place ID: {place_id} (User: {user_id}, Tenant: {tenant_id})")
    if not MAPS_CONFIGURED: # Verifica si la clave está cargada
        logger.error("/direccion/detalles llamado pero MAPS_API_ALL no está configurada.")
        raise HTTPException(status_code=503, detail="Servicio de direcciones no disponible (configuración backend).")

    GOOGLE_PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    fields_needed = "address_component,formatted_address" # 'address_component' es el typo correcto en la API
    params = {
        "place_id": place_id,
        "key": MAPS_API_ALL, # Usa la clave segura del backend
        "fields": fields_needed,
        "language": "es" # Preferir español
    }

    try:
        # Usar httpx para llamada asíncrona
        async with httpx.AsyncClient(timeout=10.0) as client_http:
            logger.debug(f"Llamando a Google Places Details API. URL: {GOOGLE_PLACES_DETAILS_URL}, Params: {params}")
            response = await client_http.get(GOOGLE_PLACES_DETAILS_URL, params=params)
            response.raise_for_status() # Lanza excepción para errores HTTP 4xx/5xx
            data = response.json()
            logger.debug(f"Respuesta JSON de Google Places API: {data}")

        # Verificar el status de la respuesta de Google API
        api_status = data.get("status")
        if api_status != "OK":
            error_message = data.get("error_message", f"Status Google API: {api_status}")
            logger.error(f"Error API Google Places para place_id {place_id}: {error_message} (Status: {api_status})")
            if api_status == "REQUEST_DENIED": raise HTTPException(status_code=403, detail=f"Acceso denegado Google Places API: {error_message}")
            elif api_status == "INVALID_REQUEST": raise HTTPException(status_code=400, detail=f"Solicitud inválida Google Places API: {error_message}")
            elif api_status == "ZERO_RESULTS": return PlaceDetailsResponse(success=False, error="No se encontraron detalles para la dirección seleccionada.")
            else: raise HTTPException(status_code=503, detail=f"Google Places API devolvió error: {api_status}")

        # Procesar resultado si status es OK
        result = data.get("result", {})
        address_components = result.get("address_components", [])
        formatted_address = result.get("formatted_address", "N/A")
        logger.info(f"Dirección formateada recibida para {place_id}: {formatted_address}")

        if not address_components:
             logger.warning(f"No se encontraron 'address_components' en la respuesta OK de Google para {place_id}")
             return PlaceDetailsResponse(success=False, error="No se recibieron componentes de dirección detallados.")

        # Extraer los datos necesarios
        street_number = None; route = None; postal_code = None; locality = None; province = None; country = None
        for component in address_components:
            types = component.get("types", []); long_name = component.get("long_name")
            if not types or not long_name: continue # Componente inválido
            # Mapeo de tipos a campos
            if "street_number" in types: street_number = long_name
            if "route" in types: route = long_name
            if "postal_code" in types: postal_code = long_name
            if "locality" in types: locality = long_name
            if "administrative_area_level_2" in types: province = long_name # Provincia
            elif "administrative_area_level_1" in types and not province: province = long_name # Fallback Comunidad Autónoma / Estado
            if "country" in types: country = long_name

        # Construir string de dirección (Calle, Número)
        street_address_parts = [];
        if route: street_address_parts.append(route)
        if street_number: street_address_parts.append(street_number)
        final_street_address = ", ".join(street_address_parts) if route and street_number else (route or street_number)

        logger.info(f"Componentes extraídos {place_id}: Calle='{final_street_address}', CP='{postal_code}', Loc='{locality}', Prov='{province}', Pais='{country}'")
        # Devolver respuesta exitosa
        return PlaceDetailsResponse(
            success=True,
            street_address=final_street_address,
            postal_code=postal_code,
            locality=locality,
            province=province,
            country=country
        )

    # Manejo de excepciones
    except httpx.TimeoutException: logger.error(f"Timeout Google Places {place_id}"); raise HTTPException(504, "Timeout obtener detalles dirección.")
    except httpx.RequestError as e: logger.error(f"Error conexión Google Places {place_id}: {e}", exc_info=True); raise HTTPException(503, "Error conexión obtener detalles dirección.")
    except HTTPException as e: raise e # Re-lanzar errores HTTP ya manejados
    except Exception as e: logger.error(f"Error inesperado detalles dirección {place_id}: {e}", exc_info=True); raise HTTPException(500, "Error interno procesar dirección.")

# --- Punto de Entrada (Uvicorn local - Mantenido comentado) ---
# if __name__ == "__main__":
#     import uvicorn
#     logger.info("Iniciando servidor Uvicorn local...")
#     port = int(os.getenv("PORT", 8000)) # Render usa PORT env var
#     # from dotenv import load_dotenv # Descomentar si usas .env localmente
#     # load_dotenv()
#     uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)

# --- FIN main.py v2.4.3-mt ---
# --- INICIO main.py v2.4.0-mt (Añade Endpoint Place Details) ---
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, Query
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
import httpx  # <--- AÑADIDO para llamadas async
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# MODIFICADO: Versión indica adición endpoint Place Details
app = FastAPI(title="Asistente IA UBIKUA API v2.4.0-mt (Place Details Endpoint)", version="2.4.0-mt")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En producción, restringe esto a tu dominio frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración (MODIFICADO: Añadida MAPS_API_ALL) ---
try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    assert openai_api_key, "Var OPENAI_API_KEY no encontrada."
    client = OpenAI(api_key=openai_api_key)
    logging.info("Cliente OpenAI OK.")

    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Para Google Search
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Google API Keys (Search) no encontradas.")

    # --- NUEVA CLAVE API PARA MAPS/PLACES ---
    MAPS_API_ALL = os.getenv("MAPS_API_ALL")
    if not MAPS_API_ALL:
        logging.warning("MAPS_API_ALL no encontrada (necesaria para Place Details).")
        MAPS_CONFIGURED = False
    else:
        MAPS_CONFIGURED = True
        logging.info("MAPS_API_ALL Key OK.")
    # --- FIN NUEVA CLAVE ---

    DB_HOST = os.getenv("DB_HOST")
    DB_USER = os.getenv("DB_USER")
    DB_PASS = os.getenv("DB_PASS")
    DB_NAME = os.getenv("DB_NAME")
    DB_PORT = int(os.getenv("DB_PORT", 5432))
    if not all([DB_HOST, DB_USER, DB_PASS, DB_NAME]):
        logging.warning("Faltan variables DB.")
        DB_CONFIGURED = False
    else:
        DB_CONFIGURED = True
        logging.info("Credenciales BD PostgreSQL OK.")

    PHP_FILE_SERVE_URL = os.getenv("PHP_FILE_SERVE_URL")
    PHP_API_SECRET_KEY = os.getenv("PHP_API_SECRET_KEY")
    if not PHP_FILE_SERVE_URL or not PHP_API_SECRET_KEY:
        logging.warning("Faltan PHP_FILE_SERVE_URL o PHP_API_SECRET_KEY (Necesario para procesar docs).")
        PHP_BRIDGE_CONFIGURED = False
    else:
        PHP_BRIDGE_CONFIGURED = True
        logging.info("Config PHP Bridge OK.")

except Exception as e:
    logging.error(f"Error Configuración Crítica: {e}", exc_info=True)
    client = None
    DB_CONFIGURED = False
    PHP_BRIDGE_CONFIGURED = False
    MAPS_CONFIGURED = False # Asegurarse de que se marca como no configurado en caso de error

# --- Modelos Pydantic (MODIFICADO: Añadidos modelos para Place Details) ---
class PeticionConsulta(BaseModel):
    mensaje: str
    especializacion: str = "general"
    buscar_web: bool = False
    user_id: int | None = None
    tenant_id: int | None = None

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

class ProcessRequest(BaseModel):
    doc_id: int
    user_id: int
    tenant_id: int | None = None

class ProcessResponse(BaseModel):
    success: bool
    message: str | None = None
    error: str | None = None

# --- Nuevos Modelos para Place Details ---
class PlaceDetailsResponse(BaseModel):
    success: bool
    postal_code: str | None = None
    locality: str | None = None # Población / Ciudad
    country: str | None = None
    error: str | None = None
# --- Fin Nuevos Modelos ---


# --- Prompts (Sin cambios) ---
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de Ashotel, un experto multidisciplinar que responde de manera "
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
    "Eres el Asistente IA oficial de Ashotel y un experto en redactar informes y análisis de documentos. "
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

# --- Temp Dir para documentos (Sin cambios) ---
TEMP_DIR = "/tmp/uploads_ashotel"
os.makedirs(TEMP_DIR, exist_ok=True)

# --- Funciones Auxiliares (Sin cambios, excepto get_db_connection revisada) ---
def get_db_connection():
    if not DB_CONFIGURED:
        logging.error("Configuración de base de datos incompleta. No se puede conectar.")
        return None
    try:
        conn = psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER,
            password=DB_PASS, port=DB_PORT, connect_timeout=5 # Timeout 5 seg
        )
        # logging.info("Conexión PostgreSQL establecida con éxito.") # Log opcional
        return conn
    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error al conectar con PostgreSQL: {error}", exc_info=True)
        return None

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    texto = ""
    logging.info(f"Extrayendo texto PDF/DOCX de: {ruta_archivo}")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo)
                if lector.is_encrypted:
                    logging.warning(f"El archivo PDF está encriptado: {ruta_archivo}. La extracción de texto puede fallar.")
                    # Intento opcional de desbloqueo si se conoce contraseña (no implementado)
                for pagina in lector.pages:
                    try:
                        texto_pagina = pagina.extract_text()
                        if texto_pagina:
                            texto += texto_pagina + "\n"
                    except Exception as page_error:
                        logging.warning(f"Error al extraer texto de una página en {ruta_archivo}: {page_error}")
                        continue # Continuar con la siguiente página
        elif extension in ["doc", "docx"]:
             try:
                doc = Document(ruta_archivo)
                texto = "\n".join([p.text for p in doc.paragraphs if p.text])
             except PackageNotFoundError:
                 logging.error(f"Error al leer DOCX {ruta_archivo}: Archivo no encontrado o formato inválido.")
                 return "[Error DOCX: Archivo inválido o no encontrado]"
             except Exception as docx_error: # Captura genérica para otros errores de python-docx
                 logging.error(f"Error inesperado al procesar DOCX {ruta_archivo}: {docx_error}", exc_info=True)
                 return "[Error interno procesando DOCX]"
        else:
            logging.error(f"Tipo de archivo no esperado '{extension}' en extraer_texto_pdf_docx")
            return "[Error interno: Tipo no esperado en extraer_texto_pdf_docx]"

        logging.info(f"Texto extraído de PDF/DOCX OK (longitud: {len(texto)} caracteres).")
        return texto.strip()

    except pdf_errors.PdfReadError as e:
        logging.error(f"Error crítico al leer PDF {ruta_archivo}: {e}")
        return "[Error PDF: No se pudo leer o archivo corrupto]"
    except FileNotFoundError:
        logging.error(f"Error interno de sistema: Archivo {ruta_archivo} no encontrado durante la extracción.")
        return "[Error interno: Archivo no encontrado]"
    except Exception as e:
        logging.error(f"Error general al extraer texto de PDF/DOCX {ruta_archivo}: {e}", exc_info=True)
        return "[Error interno procesando PDF/DOCX]"


def extraer_texto_simple(ruta_archivo: str) -> str:
    logging.info(f"Extrayendo texto simple de: {ruta_archivo}")
    texto = ""
    try:
        with open(ruta_archivo, 'rb') as fb:
            raw_data = fb.read()
            if not raw_data:
                logging.warning(f"Archivo vacío: {ruta_archivo}")
                return "" # Devolver vacío si el archivo no tiene contenido
            # logging.info(f"Leídos {len(raw_data)} bytes de {ruta_archivo}")
            detected_encoding = chardet.detect(raw_data)['encoding']
            # Usar un encoding por defecto más robusto si la detección falla o es incierta
            if not detected_encoding or detected_encoding.lower() == 'ascii':
                detected_encoding = 'utf-8' # utf-8 como fallback común
                logging.info(f"Detección de encoding incierta o ASCII, usando '{detected_encoding}' por defecto.")
            else:
                logging.info(f"Encoding detectado: {detected_encoding}")

        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f:
            texto = f.read()
        logging.info(f"Texto extraído simple OK (longitud: {len(texto)} caracteres).")
        return texto.strip()

    except FileNotFoundError:
        logging.error(f"Error interno: Archivo {ruta_archivo} no encontrado para extracción simple.")
        return "[Error interno: Archivo no encontrado]"
    except UnicodeDecodeError as ude:
         logging.error(f"Error de decodificación para {ruta_archivo} con encoding {detected_encoding}: {ude}")
         # Intento con otro encoding común como fallback
         try:
             logging.info(f"Intentando decodificar {ruta_archivo} con ISO-8859-1...")
             with open(ruta_archivo, 'r', encoding='iso-8859-1', errors='ignore') as f_fallback:
                 texto = f_fallback.read()
             logging.info(f"Texto extraído simple con fallback ISO-8859-1 OK (longitud: {len(texto)} caracteres).")
             return texto.strip()
         except Exception as e_fallback:
             logging.error(f"Fallo el fallback de decodificación para {ruta_archivo}: {e_fallback}")
             return "[Error de Codificación: No se pudo leer el texto]"
    except Exception as e:
        logging.error(f"Error inesperado al extraer texto simple de {ruta_archivo}: {e}", exc_info=True)
        return "[Error interno procesando texto plano]"


def buscar_google(query: str) -> str:
    # Misma función que antes
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "<p><i>[Búsqueda web no disponible (sin configurar).]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3}
    logging.info(f"Buscando en Google (Search): '{query}'")
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        resultados = data.get("items", [])
        if not resultados:
            return "<p><i>[No se encontraron resultados web.]</i></p>"
        texto_resultados = "<div class='google-results' style='margin-top:15px; padding-top:10px; border-top: 1px solid #eee;'>"
        texto_resultados += "<h4 style='font-size:0.9em;color:#555; margin-bottom: 5px;'>Resultados web relacionados:</h4><ul>"
        for item in resultados:
            title = item.get('title','')
            link = item.get('link','#')
            snippet = item.get('snippet','').replace('\n',' ')
            # Usar listas para mejor formato HTML
            texto_resultados += f"<li style='margin-bottom: 8px;'><a href='{link}' target='_blank' style='font-weight: bold; text-decoration: underline;'>{title}</a><p style='font-size: 0.85em; margin-top: 2px; color: #333;'>{snippet}</p><cite style='font-size: 0.8em; color: #777; display: block;'>{link}</cite></li>"
        texto_resultados += "</ul></div>"
        logging.info(f"Búsqueda web OK: {len(resultados)} resultados.")
        return texto_resultados
    except requests.exceptions.Timeout:
        logging.error("Timeout durante la búsqueda web.")
        return "<p><i>[Error: Timeout en búsqueda web.]</i></p>"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error en la solicitud de búsqueda web: {e}")
        # Evitar mostrar detalles de la URL o clave en el error al usuario
        return "<p><i>[Error de conexión durante la búsqueda web.]</i></p>"
    except Exception as e:
        logging.error(f"Error inesperado durante la búsqueda web: {e}", exc_info=True)
        return "<p><i>[Error inesperado durante la búsqueda web.]</i></p>"

# --- Endpoint /process-document (Sin cambios funcionales respecto a v2.3.7-mt) ---
@app.post("/process-document", response_model=ProcessResponse)
async def process_document_text(request: ProcessRequest):
    doc_id = request.doc_id
    current_user_id = request.user_id
    current_tenant_id = request.tenant_id

    if not current_user_id or not current_tenant_id:
        logging.error(f"Falta user_id ({current_user_id}) o tenant_id ({current_tenant_id}) en /process-document para doc {doc_id}")
        return ProcessResponse(success=False, error="Faltan IDs de usuario o tenant.")

    logging.info(f"Procesando documento ID: {doc_id} (Usuario: {current_user_id}, Tenant: {current_tenant_id})")

    if not DB_CONFIGURED or not PHP_BRIDGE_CONFIGURED:
        error_msg = "Configuración incompleta en el backend."
        if not DB_CONFIGURED: error_msg += " (BD)"
        if not PHP_BRIDGE_CONFIGURED: error_msg += " (PHP Bridge)"
        logging.error(error_msg + f" para doc {doc_id}")
        return ProcessResponse(success=False, error=error_msg)

    conn = None
    original_fname = None
    temp_path = None
    try:
        # 1. Obtener info del documento (Filtrando por tenant_id)
        conn = get_db_connection()
        if not conn: raise ConnectionError("No se pudo conectar a la base de datos para obtener info del documento.")

        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            sql_select = """
                SELECT original_filename, file_type, stored_path
                FROM user_documents
                WHERE id = %s AND user_id = %s AND tenant_id = %s
            """
            cursor.execute(sql_select, (doc_id, current_user_id, current_tenant_id))
            doc_info = cursor.fetchone()
            if not doc_info:
                raise FileNotFoundError(f"Documento ID {doc_id} no encontrado para el usuario {current_user_id} y tenant {current_tenant_id}.")

            original_fname = doc_info['original_filename']
            file_type = doc_info['file_type']
            stored_path = doc_info['stored_path'] # Podría usarse si el acceso es directo, pero usamos PHP Bridge
            logging.info(f"Info obtenida para doc {doc_id}: fname='{original_fname}', type='{file_type}'")


        # 2. Obtener contenido vía PHP Bridge (incluir tenant_id en la URL)
        # Asegúrate que PHP_FILE_SERVE_URL termina en '?' o '&' según tu script PHP
        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&tenant_id={current_tenant_id}&api_key={PHP_API_SECRET_KEY}"
        logging.info(f"Solicitando contenido de doc ID {doc_id} al PHP Bridge. URL: {serve_url}")

        # Usar requests síncrono aquí está bien si el proceso es llamado en background
        # Si es llamado directamente y puede tardar, considerar async con httpx
        response = requests.get(serve_url, timeout=60, stream=True) # Timeout aumentado a 60s
        response.raise_for_status() # Lanza excepción para códigos 4xx/5xx

        # Loguear snippet del contenido recibido
        try:
            content_snippet = b''
            bytes_read = 0
            # Leer los primeros bytes sin consumir todo el stream si es grande
            for chunk in response.iter_content(chunk_size=512, decode_unicode=False):
                 content_snippet += chunk
                 bytes_read += len(chunk)
                 if bytes_read >= 512:
                     break
            snippet_decoded = content_snippet[:512].decode('utf-8', 'ignore') # Decodificar solo el snippet
            logging.info(f"Contenido recibido del PHP Bridge (primeros {len(snippet_decoded)} caracteres decodificados): {snippet_decoded}...")
        except Exception as log_exc:
            snippet_decoded = "[Error decodificando snippet]"
            logging.error(f"Error decodificando snippet inicial: {log_exc}")

        # 3. Guardar temporalmente el contenido completo y extraer texto
        file_ext = os.path.splitext(original_fname)[1].lower().strip('.') if original_fname else ''
        extracted_text = None
        TEXT_EXTENSIONS_PROC = ["pdf", "doc", "docx", "txt", "csv"] # Extensiones que procesamos

        if file_ext in TEXT_EXTENSIONS_PROC:
            # Crear archivo temporal de forma segura
            with tempfile.NamedTemporaryFile(mode='wb', suffix=f'.{file_ext}', dir=TEMP_DIR, delete=False) as temp_file:
                temp_path = temp_file.name # Guardar el path para usarlo después y para borrarlo
                logging.info(f"Guardando contenido en archivo temporal: {temp_path}")
                # Escribir el contenido del stream al archivo temporal
                # Importante: response.content ya estaría completo si no usamos stream=True antes
                # Re-obtener o iterar de nuevo si es necesario, o usar response.raw
                try:
                    # Iterar sobre el stream para escribir en el archivo temporal
                     bytes_written = 0
                     # Necesitamos asegurar que el stream no se ha consumido ya por el log del snippet
                     # Si usamos iter_content arriba, debemos volver a obtener la response o usar response.raw
                     # Reintentamos la request para estar seguros (no ideal, pero asegura contenido completo)
                     response_full = requests.get(serve_url, timeout=60, stream=True)
                     response_full.raise_for_status()
                     for chunk in response_full.iter_content(chunk_size=8192):
                         temp_file.write(chunk)
                         bytes_written += len(chunk)
                     logging.info(f"Escritos {bytes_written} bytes en {temp_path}")

                except Exception as write_err:
                     logging.error(f"Error escribiendo en archivo temporal {temp_path}: {write_err}", exc_info=True)
                     raise IOError(f"No se pudo escribir el archivo temporal para {original_fname}")

            # Ahora que el archivo está escrito y cerrado, procedemos a extraer texto
            logging.info(f"Archivo temporal {temp_path} escrito y cerrado. Extrayendo texto...")
            if file_ext in ['pdf', 'doc', 'docx']:
                extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
            elif file_ext in ['txt', 'csv']:
                extracted_text = extraer_texto_simple(temp_path)

            logging.info(f"Texto extraído (longitud: {len(extracted_text) if extracted_text else 0} caracteres)")

        else:
             logging.warning(f"Extracción no soportada para la extensión '{file_ext}' del archivo {original_fname}")
             extracted_text = f"[Extracción no soportada para tipo de archivo: {file_ext}]"


        # Validar texto extraído
        if extracted_text is None or extracted_text.strip() == "" or extracted_text.strip().startswith("[Error"):
             logging.error(f"Extracción de texto fallida o vacía para doc {doc_id}. Texto: '{extracted_text}'")
             # Marcar como procesado pero con error en el texto para no reintentar indefinidamente? O devolver error?
             # Por ahora, actualizamos la BD marcando como procesado y guardando el mensaje de error/vacío.
             if extracted_text is None: extracted_text = "[Error: Extracción devolvió None]"
             if extracted_text.strip() == "": extracted_text = "[Archivo vacío o sin texto extraíble]"


        # 4. Actualizar BD (Filtrando por tenant_id)
        logging.info(f"Actualizando BD para doc ID {doc_id} / tenant {current_tenant_id}...")
        if not conn or conn.closed: # Re-verificar conexión
             conn = get_db_connection()
             if not conn: raise ConnectionError("No se pudo reconectar a la base de datos para actualizar.")

        with conn.cursor() as cursor:
            sql_update = """
                UPDATE user_documents
                SET extracted_text = %s, procesado = TRUE
                WHERE id = %s AND user_id = %s AND tenant_id = %s
            """
            # Asegurar que el texto no sea excesivamente largo para la BD (si hay límite)
            MAX_TEXT_LENGTH = 10 * 1024 * 1024 # Límite ejemplo 10MB (ajustar según BD)
            if len(extracted_text) > MAX_TEXT_LENGTH:
                 logging.warning(f"Texto extraído truncado a {MAX_TEXT_LENGTH} caracteres para BD (doc {doc_id}).")
                 extracted_text = extracted_text[:MAX_TEXT_LENGTH]

            cursor.execute(sql_update, (extracted_text, doc_id, current_user_id, current_tenant_id))
            if cursor.rowcount == 0:
                # Esto podría pasar si el doc fue borrado o modificado entre la selección inicial y el update
                logging.warning(f"UPDATE no afectó filas. Doc {doc_id}/Tenant {current_tenant_id} podría haber cambiado.")
                # Considerar si esto debe ser un error o solo un warning. Por ahora, warning.
        conn.commit()
        logging.info(f"Base de datos actualizada con éxito para doc ID {doc_id}.")
        return ProcessResponse(success=True, message="Documento procesado y texto extraído.")

    except FileNotFoundError as e:
         logging.error(f"Error: Documento no encontrado en BD para ID {doc_id}, User {current_user_id}, Tenant {current_tenant_id}. Detalles: {e}")
         return ProcessResponse(success=False, error=str(e))
    except ConnectionError as e: # Error nuestro de conexión BD
         logging.error(f"Error de conexión a BD procesando doc {doc_id}: {e}", exc_info=True)
         return ProcessResponse(success=False, error=f"Error de conexión a la base de datos: {e}")
    except requests.exceptions.RequestException as e:
        logging.error(f"Error al conectar con PHP Bridge para doc {doc_id}: {e}", exc_info=True)
        status_code = e.response.status_code if e.response is not None else 'N/A'
        return ProcessResponse(success=False, error=f"Error al obtener el archivo desde PHP (Código: {status_code}). Verifica la URL y el script PHP.")
    except IOError as e: # Error de escritura/lectura de archivo temporal
         logging.error(f"Error de I/O con archivo temporal para doc {doc_id}: {e}", exc_info=True)
         return ProcessResponse(success=False, error=f"Error al manejar archivo temporal: {e}")
    except (Exception, psycopg2.Error) as e: # Otros errores BD o generales
        logging.error(f"Error general procesando doc {doc_id}: {e}", exc_info=True)
        if conn and not conn.closed:
            try:
                conn.rollback() # Revertir transacción si hubo error BD
            except Exception as rb_err:
                 logging.error(f"Error haciendo rollback: {rb_err}")
        return ProcessResponse(success=False, error=f"Error interno del servidor durante el procesamiento: {type(e).__name__}")
    finally:
        # Limpieza final del archivo temporal si existe
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
                logging.info(f"Archivo temporal {temp_path} eliminado en bloque finally.")
            except OSError as e_remove:
                logging.error(f"Error al borrar archivo temporal {temp_path} en finally: {e_remove}")
        # Cerrar conexión BD
        if conn and not conn.closed:
            conn.close()
            # logging.info("Conexión a BD cerrada en finally.")


# --- Endpoint de consulta (/consulta - Sin cambios funcionales respecto a v2.3.7-mt) ---
@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    if not client:
        raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    current_user_id = datos.user_id
    current_tenant_id = datos.tenant_id
    if not current_user_id or not current_tenant_id:
        logging.error("Llamada a /consulta sin user_id o tenant_id válidos.")
        raise HTTPException(status_code=400, detail="User ID y Tenant ID son requeridos.")

    especializacion = datos.especializacion.lower() if datos.especializacion else "general"
    mensaje_usuario = datos.mensaje
    forzar_busqueda_web = datos.buscar_web
    logging.info(f"Consulta recibida: User={current_user_id}, Tenant={current_tenant_id}, Especialización='{especializacion}', BuscarWeb={forzar_busqueda_web}, Mensaje='{mensaje_usuario[:100]}...'")

    # --- Obtener prompt personalizado ---
    custom_prompt_text = ""
    conn_prompt = get_db_connection()
    if conn_prompt:
        try:
            with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                sql_prompt = "SELECT custom_prompt FROM user_settings WHERE user_id = %s AND tenant_id = %s"
                cursor.execute(sql_prompt, (current_user_id, current_tenant_id))
                result = cursor.fetchone()
                if result and result.get('custom_prompt'):
                    custom_prompt_text = result['custom_prompt'].strip()
                    logging.info(f"Prompt personalizado encontrado para user {current_user_id}/tenant {current_tenant_id}.")
                else:
                     logging.info(f"No se encontró prompt personalizado para user {current_user_id}/tenant {current_tenant_id}.")
        except (Exception, psycopg2.Error) as e:
            logging.error(f"Error al obtener prompt personalizado de BD para user {current_user_id}/tenant {current_tenant_id}: {e}", exc_info=True)
        finally:
            conn_prompt.close()
    else:
        logging.warning(f"No se pudo establecer conexión a BD para obtener prompt personalizado (user {current_user_id}/tenant {current_tenant_id}).")


    # --- Obtener contexto documental (RAG) ---
    document_context = ""
    MAX_RAG_TOKENS = 3500 # Límite de tokens para el contexto RAG
    conn_docs = get_db_connection()
    if conn_docs:
        try:
            with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                # Limpiar y preparar el query de búsqueda FTS
                # Eliminar caracteres especiales comunes que puedan romper tsquery
                search_query_cleaned = re.sub(r'[!\'()|&:*]', ' ', mensaje_usuario).strip()
                 # Convertir espacios múltiples a un solo espacio y luego a formato tsquery ' & '
                search_query_terms = search_query_cleaned.split()
                if not search_query_terms:
                     logging.info(f"Mensaje de usuario vacío o solo caracteres especiales, no se busca en RAG.")
                else:
                    fts_query_string = ' & '.join(search_query_terms)
                    logging.info(f"Ejecutando búsqueda FTS con query: '{fts_query_string}' para user {current_user_id}/tenant {current_tenant_id}")

                    sql_fts = """
                        SELECT original_filename, extracted_text,
                               ts_rank(fts_vector, plainto_tsquery('spanish', %s)) as relevance
                        FROM user_documents
                        WHERE user_id = %s AND tenant_id = %s
                          AND is_active_for_ai = TRUE
                          AND fts_vector @@ plainto_tsquery('spanish', %s)
                          AND extracted_text IS NOT NULL AND extracted_text != '' AND NOT extracted_text LIKE '[Error%%'
                        ORDER BY relevance DESC
                        LIMIT 5 -- Obtener más documentos y filtrar por tokens
                    """
                    cursor.execute(sql_fts, (fts_query_string, current_user_id, current_tenant_id, fts_query_string))
                    relevant_docs = cursor.fetchall()

                    if relevant_docs:
                        logging.info(f"Encontrados {len(relevant_docs)} documentos RAG potenciales para user {current_user_id}/tenant {current_tenant_id}")
                        context_parts = ["\n\n### Contexto Relevante de Documentos del Usuario ###"]
                        current_token_count = 0
                        docs_included_count = 0

                        for doc in relevant_docs:
                            filename = doc['original_filename']
                            text = doc['extracted_text']
                            # Estimación simple de tokens (puede ser reemplazada por tiktoken si es necesario)
                            doc_tokens_estimated = len(text.split()) * 1.3

                            if current_token_count + doc_tokens_estimated <= MAX_RAG_TOKENS:
                                context_parts.append(f"\n--- Inicio Documento: {filename} ---")
                                context_parts.append(text)
                                context_parts.append(f"--- Fin Documento: {filename} ---")
                                current_token_count += doc_tokens_estimated
                                docs_included_count += 1
                                if current_token_count >= MAX_RAG_TOKENS:
                                     logging.warning(f"Alcanzado límite de tokens RAG ({MAX_RAG_TOKENS}) tras incluir {docs_included_count} documentos.")
                                     break # Salir si ya llenamos el cupo
                            else:
                                # Intentar añadir solo una porción del documento si cabe algo
                                remaining_tokens = MAX_RAG_TOKENS - current_token_count
                                if remaining_tokens > 100: # Añadir solo si cabe una porción significativa
                                     available_chars = int(remaining_tokens / 1.3)
                                     context_parts.append(f"\n--- Inicio Documento (parcial): {filename} ---")
                                     context_parts.append(text[:available_chars] + "...")
                                     context_parts.append(f"--- Fin Documento (parcial): {filename} ---")
                                     current_token_count += remaining_tokens
                                     docs_included_count += 1
                                     logging.warning(f"Incluida porción parcial del doc {filename}. Alcanzado límite de tokens RAG ({MAX_RAG_TOKENS}).")
                                else:
                                     logging.info(f"Documento {filename} omitido por exceder límite de tokens RAG.")
                                break # Salir tras añadir porción o si no cabe nada significativo

                        if docs_included_count > 0:
                           document_context = "\n".join(context_parts)
                           logging.info(f"Contexto RAG construido con {docs_included_count} documentos ({current_token_count:.0f} tokens estimados) para user {current_user_id}/tenant {current_tenant_id}")
                        else:
                             logging.info(f"Ningún documento RAG añadido al contexto (límite de tokens o sin resultados relevantes) para user {current_user_id}/tenant {current_tenant_id}")
                    else:
                        logging.info(f"No se encontraron documentos RAG relevantes para la consulta user {current_user_id}/tenant {current_tenant_id}")
        except (Exception, psycopg2.Error) as e:
            logging.error(f"Error al obtener contexto RAG de BD para user {current_user_id}/tenant {current_tenant_id}: {e}", exc_info=True)
        finally:
            if conn_docs:
                conn_docs.close()
    else:
        logging.warning(f"No se pudo establecer conexión a BD para obtener contexto RAG (user {current_user_id}/tenant {current_tenant_id}).")


    # --- Combinar prompts ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico]
    if custom_prompt_text:
        system_prompt_parts.append(f"\n\n### Instrucciones Adicionales Proporcionadas por el Usuario (Memoria) ###\n{custom_prompt_text}")
    if document_context:
        system_prompt_parts.append(document_context) # Ya tiene su propio encabezado

    system_prompt = "\n".join(filter(None, system_prompt_parts))
    # logging.debug(f"System Prompt Final para OpenAI:\n------\n{system_prompt}\n------") # Log completo opcional


    # --- Llamada a OpenAI ---
    texto_respuesta_final = ""
    MAX_RETRIES_OPENAI = 2
    for attempt in range(MAX_RETRIES_OPENAI):
         try:
            logging.info(f"Llamando a OpenAI (Intento {attempt + 1}/{MAX_RETRIES_OPENAI})...")
            respuesta_inicial = client.chat.completions.create(
                model="gpt-4-turbo", # Asegúrate que este modelo está disponible
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": mensaje_usuario}
                ],
                temperature=0.5,
                max_tokens=2000 # Aumentado ligeramente por si RAG es grande
            )
            texto_respuesta_final = respuesta_inicial.choices[0].message.content.strip()
            logging.info(f"Respuesta recibida de OpenAI (longitud: {len(texto_respuesta_final)}).")

            # --- Búsqueda web si es necesario ---
            # Comprobar si la respuesta sugiere falta de información o si se forzó
            respuesta_lower = texto_respuesta_final.lower()
            necesita_web = any(frase in respuesta_lower for frase in FRASES_BUSQUEDA) or forzar_busqueda_web

            if necesita_web:
                logging.info(f"Respuesta sugiere búsqueda web o fue forzada (Forzado: {forzar_busqueda_web}). Realizando búsqueda...")
                web_resultados_html = buscar_google(mensaje_usuario)
                # Añadir resultados web de forma clara
                if web_resultados_html and not web_resultados_html.startswith("<p><i>["):
                     texto_respuesta_final += "\n\n" + web_resultados_html
                     logging.info("Resultados de búsqueda web añadidos a la respuesta.")
                else:
                     logging.info("Búsqueda web no produjo resultados o hubo un error.")
            else:
                 logging.info("No se necesita búsqueda web para esta respuesta.")

            # Si todo fue bien, salimos del bucle de reintentos
            break

         except APIError as e:
            logging.error(f"Error API OpenAI en /consulta (Intento {attempt + 1}): {e}")
            if attempt == MAX_RETRIES_OPENAI - 1: # Si es el último intento
                 raise HTTPException(status_code=503, detail=f"Error OpenAI: {e.message} (tras {MAX_RETRIES_OPENAI} intentos)")
            # Esperar un poco antes de reintentar
            # time.sleep(1) # Necesitarías importar time
         except Exception as e:
            logging.error(f"Error inesperado en /consulta (Intento {attempt + 1}): {e}", exc_info=True)
            if attempt == MAX_RETRIES_OPENAI - 1:
                 raise HTTPException(status_code=500, detail=f"Error interno del servidor durante la consulta (tras {MAX_RETRIES_OPENAI} intentos)")
            # time.sleep(1)

    # Limpieza final de la respuesta (opcional)
    # Podrías quitar tags <script> o similar si OpenAI los añade por error
    # texto_respuesta_final = limpiar_html(texto_respuesta_final)

    return RespuestaConsulta(respuesta=texto_respuesta_final)


# --- Endpoint /analizar-documento (Sin cambios funcionales respecto a v2.3.7-mt) ---
@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general"),
    user_id: int | None = Form(None),
    tenant_id: int | None = Form(None)
):
    if not client:
        raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    current_user_id = user_id
    current_tenant_id = tenant_id
    if not current_user_id or not current_tenant_id:
        logging.error("Llamada a /analizar-documento sin user_id o tenant_id válidos.")
        raise HTTPException(status_code=400, detail="User ID y Tenant ID son requeridos para análisis.")

    # --- Mismo código que v2.3.7-mt para procesar el archivo y llamar a OpenAI ---
    filename = file.filename or "unknown"
    content_type = file.content_type or ""
    extension = filename.split('.')[-1].lower() if '.' in filename else ''
    especializacion_lower = especializacion.lower() if especializacion else "general"
    logging.info(f"Análisis Documento: User={current_user_id}, Tenant={current_tenant_id}, File='{filename}', Espec='{especializacion_lower}'")

    # Obtener prompt personalizado (igual que en /consulta)
    custom_prompt_text = ""
    conn_prompt = get_db_connection()
    if conn_prompt:
        try:
            with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                sql_prompt = "SELECT custom_prompt FROM user_settings WHERE user_id = %s AND tenant_id = %s"
                cursor.execute(sql_prompt, (current_user_id, current_tenant_id))
                result = cursor.fetchone()
                if result and result.get('custom_prompt'):
                    custom_prompt_text = result['custom_prompt'].strip()
                    logging.info(f"Prompt personalizado encontrado para análisis user {current_user_id}/tenant {current_tenant_id}.")
        except (Exception, psycopg2.Error) as e:
             logging.error(f"Error al obtener prompt personalizado (análisis) de BD para user {current_user_id}/tenant {current_tenant_id}: {e}", exc_info=True)
        finally:
            conn_prompt.close()
    else:
        logging.warning(f"No se pudo establecer conexión a BD para obtener prompt personalizado (análisis, user {current_user_id}/tenant {current_tenant_id}).")

    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico]
    if custom_prompt_text:
        system_prompt_parts.append(f"\n\n### Instrucciones Adicionales Proporcionadas por el Usuario (Memoria) ###\n{custom_prompt_text}")
    system_prompt = "\n".join(filter(None, system_prompt_parts))
    # logging.debug(f"System Prompt Final para Análisis:\n------\n{system_prompt}\n------")

    messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]
    TEXT_EXTENSIONS = ["pdf", "doc", "docx", "txt", "csv"]
    temp_filename_analisis = None # Para asegurar limpieza

    try:
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando imagen '{filename}' ({content_type}) para análisis con GPT-4 Vision.")
            image_bytes = await file.read()
            # Validar tamaño si es necesario
            MAX_IMAGE_SIZE = 20 * 1024 * 1024 # Límite OpenAI 20MB
            if len(image_bytes) > MAX_IMAGE_SIZE:
                 logging.error(f"Imagen '{filename}' excede el límite de {MAX_IMAGE_SIZE / (1024*1024)} MB.")
                 raise HTTPException(status_code=413, detail=f"La imagen excede el tamaño máximo permitido ({MAX_IMAGE_SIZE / (1024*1024)} MB).")

            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_prompt = "Analiza detalladamente la siguiente imagen y genera un informe profesional y bien estructurado en formato HTML sobre su contenido, propósito y puntos clave."
            messages_payload = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}}
                ]}
            ]
        elif extension in TEXT_EXTENSIONS:
            logging.info(f"Procesando archivo de texto '{filename}' ({extension.upper()}) para análisis.")
            texto_extraido = ""
            # Guardar temporalmente de forma segura
            with tempfile.NamedTemporaryFile(mode='wb', suffix=f'.{extension}', dir=TEMP_DIR, delete=False) as temp_file:
                 temp_filename_analisis = temp_file.name
                 try:
                     shutil.copyfileobj(file.file, temp_file)
                     logging.info(f"Archivo '{filename}' guardado temporalmente como '{temp_filename_analisis}'")
                 except Exception as copy_err:
                     logging.error(f"Error al copiar el archivo subido a temporal '{temp_filename_analisis}': {copy_err}", exc_info=True)
                     raise HTTPException(status_code=500, detail="Error al guardar el archivo temporalmente.")

            # Extraer texto del archivo temporal guardado
            try:
                 if extension in ['pdf', 'doc', 'docx']:
                    texto_extraido = extraer_texto_pdf_docx(temp_filename_analisis, extension)
                 else: # txt, csv
                    texto_extraido = extraer_texto_simple(temp_filename_analisis)
            finally:
                 # Borrar el archivo temporal después de la extracción
                 if temp_filename_analisis and os.path.exists(temp_filename_analisis):
                     try:
                         os.remove(temp_filename_analisis)
                         logging.info(f"Archivo temporal '{temp_filename_analisis}' eliminado tras extracción.")
                     except OSError as e_remove:
                         logging.error(f"Error al eliminar archivo temporal '{temp_filename_analisis}': {e_remove}")

            # Validar texto extraído
            if texto_extraido.startswith("[Error"):
                logging.error(f"Error durante la extracción de texto para '{filename}': {texto_extraido}")
                raise HTTPException(status_code=400, detail=f"Error al extraer texto del documento: {texto_extraido}")
            if not texto_extraido:
                logging.warning(f"No se extrajo texto del documento '{filename}' (puede estar vacío o no contener texto).")
                raise HTTPException(status_code=400, detail=f"No se pudo extraer contenido textual del archivo '{filename}'.")

            user_prompt = f"Redacta un informe HTML profesional y bien estructurado basado en el siguiente texto extraído del documento '{filename}':\n\n--- INICIO CONTENIDO DOCUMENTO ---\n{texto_extraido}\n--- FIN CONTENIDO DOCUMENTO ---"
            messages_payload = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ]
        else:
            # Tipo de archivo no soportado
            logging.error(f"Tipo de archivo no soportado para análisis: '{content_type or extension}' (Archivo: '{filename}')")
            raise HTTPException(status_code=415, detail=f"Tipo de archivo '{content_type or extension}' no soportado para análisis directo.")

        if not messages_payload:
             logging.error("No se generó el payload para OpenAI en /analizar-documento.")
             raise HTTPException(status_code=500, detail="Error interno: No se pudo preparar la solicitud para la IA.")

        # Llamada a OpenAI (con reintentos)
        informe_html = ""
        MAX_RETRIES_OPENAI = 2
        for attempt in range(MAX_RETRIES_OPENAI):
             try:
                 logging.info(f"Llamando a OpenAI para análisis de documento (Intento {attempt + 1}/{MAX_RETRIES_OPENAI})...")
                 respuesta_informe = client.chat.completions.create(
                     model="gpt-4-turbo", # Usar el modelo adecuado (puede ser gpt-4-vision-preview para imágenes)
                     messages=messages_payload,
                     temperature=0.3,
                     max_tokens=3000 # Ajustar según necesidad
                 )
                 informe_html = respuesta_informe.choices[0].message.content.strip()
                 logging.info(f"Informe generado por OpenAI (longitud: {len(informe_html)}).")
                 break # Salir si la llamada fue exitosa
             except APIError as e:
                 logging.error(f"Error API OpenAI en /analizar (Intento {attempt + 1}): {e}")
                 if attempt == MAX_RETRIES_OPENAI - 1:
                      raise HTTPException(status_code=503, detail=f"Error OpenAI: {e.message} (tras {MAX_RETRIES_OPENAI} intentos)")
                 # time.sleep(1)
             except Exception as e:
                 logging.error(f"Error inesperado llamando a OpenAI en /analizar (Intento {attempt + 1}): {e}", exc_info=True)
                 if attempt == MAX_RETRIES_OPENAI - 1:
                      raise HTTPException(status_code=500, detail=f"Error interno del servidor durante el análisis (tras {MAX_RETRIES_OPENAI} intentos)")
                 # time.sleep(1)


        # Limpieza básica del HTML generado (opcional)
        if BS4_AVAILABLE:
            try:
                # Quitar doctype, html, head si OpenAI los añade por error
                if "<!DOCTYPE html>" in informe_html or "<html" in informe_html:
                    soup = BeautifulSoup(informe_html, 'html.parser')
                    if soup.body:
                         informe_html = soup.body.decode_contents()
                         logging.info("Detectado HTML completo, se ha extraído solo el contenido del body.")
                    # Si no hay body pero sí html, intentar obtener el contenido interno
                    elif soup.html:
                         informe_html = soup.html.decode_contents()
                         logging.info("Detectado tag <html>, se ha extraído su contenido.")

                # Asegurarse que no empieza o termina con ```html ... ```
                informe_html = re.sub(r'^```html\s*', '', informe_html, flags=re.IGNORECASE)
                informe_html = re.sub(r'\s*```$', '', informe_html)

            except Exception as e_bs4:
                logging.error(f"Error al procesar/limpiar HTML con BeautifulSoup: {e_bs4}")
        # Asegurar que la respuesta sea al menos un párrafo si no es HTML válido
        if not re.search(r'<[a-z][\s\S]*>', informe_html, re.IGNORECASE):
            logging.warning("La respuesta de OpenAI no parece ser HTML válido, envolviendo en <p>.")
            informe_html = f"<p>{informe_html}</p>"

        return RespuestaAnalisis(informe=informe_html)

    # Captura general de errores del endpoint /analizar-documento
    except HTTPException as e:
        # Re-lanzar excepciones HTTP ya generadas
        raise e
    except Exception as e:
        logging.error(f"Error general inesperado en /analizar-documento para archivo '{filename}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor al procesar el archivo.")
    finally:
         # Asegurarse de cerrar el archivo subido
         await file.close()
         # Borrar archivo temporal si por alguna razón no se borró antes (doble check)
         if temp_filename_analisis and os.path.exists(temp_filename_analisis):
             try:
                 os.remove(temp_filename_analisis)
                 logging.info(f"Archivo temporal '{temp_filename_analisis}' verificado y eliminado en finally.")
             except OSError as e_final_remove:
                 logging.error(f"Error al eliminar archivo temporal '{temp_filename_analisis}' en finally: {e_final_remove}")


# --- NUEVO ENDPOINT: /direccion/detalles/{place_id} ---
@app.get("/direccion/detalles/{place_id}", response_model=PlaceDetailsResponse)
async def obtener_detalles_direccion(
    place_id: str,
    user_id: int | None = Query(None, description="ID del usuario que realiza la solicitud (opcional para logging)"),
    tenant_id: int | None = Query(None, description="ID del tenant del usuario (opcional para logging)")
):
    """
    Obtiene detalles de una dirección (código postal, localidad, país)
    a partir de un Place ID de Google Places.
    Utiliza la API Key segura del backend.
    """
    logging.info(f"Solicitud detalles para Place ID: {place_id} (User: {user_id}, Tenant: {tenant_id})")

    if not MAPS_CONFIGURED or not MAPS_API_ALL:
        logging.error("Intento de llamada a /direccion/detalles sin MAPS_API_ALL configurada.")
        # No devolver HTTPException 503 directamente para no exponer problemas internos,
        # mejor devolver una respuesta controlada indicando fallo.
        return PlaceDetailsResponse(success=False, error="Servicio de direcciones no disponible (configuración).")

    GOOGLE_PLACES_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    # Campos que necesitamos para rellenar el formulario
    fields_needed = "address_components,formatted_address" # formatted_address es útil para logs

    params = {
        "place_id": place_id,
        "key": MAPS_API_ALL,
        "fields": fields_needed,
        "language": "es" # Pedir resultados en español
    }

    try:
        # Usar httpx para llamada asíncrona
        async with httpx.AsyncClient(timeout=10.0) as client_http: # Timeout de 10 segundos
            response = await client_http.get(GOOGLE_PLACES_DETAILS_URL, params=params)
            response.raise_for_status() # Lanza excepción para errores 4xx/5xx HTTP

            data = response.json()

            # Verificar el estado de la respuesta de la API de Google
            api_status = data.get("status")
            if api_status != "OK":
                error_message = data.get("error_message", f"Estado API Google Places: {api_status}")
                logging.error(f"Error API Google Places para place_id {place_id}: {error_message} (Status: {api_status})")
                # Devolver error específico si es posible
                if api_status == "REQUEST_DENIED":
                     # Podría ser problema de API Key inválida o no habilitada
                     return PlaceDetailsResponse(success=False, error="Acceso denegado por Google Places API. Verifica la clave y permisos.")
                elif api_status == "INVALID_REQUEST":
                      return PlaceDetailsResponse(success=False, error="Solicitud inválida a Google Places API (posiblemente place_id incorrecto).")
                else: # ZERO_RESULTS, OVER_QUERY_LIMIT, UNKNOWN_ERROR
                     return PlaceDetailsResponse(success=False, error=f"Google Places API devolvió: {api_status}")

            # Procesar resultado si status es OK
            result = data.get("result", {})
            address_components = result.get("address_components", [])
            formatted_address = result.get("formatted_address", "N/A")
            logging.info(f"Dirección formateada recibida para {place_id}: {formatted_address}")

            if not address_components:
                 logging.warning(f"No se encontraron 'address_components' en la respuesta de Google para {place_id}")
                 return PlaceDetailsResponse(success=False, error="No se recibieron componentes de dirección detallados.")

            # Extraer los datos necesarios
            postal_code = None
            locality = None
            country = None

            for component in address_components:
                types = component.get("types", [])
                long_name = component.get("long_name")
                if not types or not long_name: continue # Saltar componente inválido

                if "postal_code" in types:
                    postal_code = long_name
                if "locality" in types: # 'locality' suele ser la ciudad/población principal
                    locality = long_name
                # A veces la ciudad está en 'administrative_area_level_3' o incluso '_2' si 'locality' falta
                elif "administrative_area_level_3" in types and not locality:
                     locality = longName
                elif "administrative_area_level_2" in types and not locality: # Como fallback menos preciso (Provincia?)
                     locality = longName

                if "country" in types:
                    country = long_name

            logging.info(f"Componentes extraídos para {place_id}: CP='{postal_code}', Loc='{locality}', Pais='{country}'")

            return PlaceDetailsResponse(
                success=True,
                postal_code=postal_code,
                locality=locality,
                country=country
            )

    except httpx.TimeoutException:
        logging.error(f"Timeout al contactar Google Places API para place_id {place_id}")
        return PlaceDetailsResponse(success=False, error="Timeout al obtener detalles de la dirección.")
    except httpx.RequestError as e:
        logging.error(f"Error de conexión al contactar Google Places API para place_id {place_id}: {e}", exc_info=True)
        return PlaceDetailsResponse(success=False, error="Error de conexión al obtener detalles de la dirección.")
    except Exception as e:
        logging.error(f"Error inesperado procesando detalles de dirección para place_id {place_id}: {e}", exc_info=True)
        return PlaceDetailsResponse(success=False, error="Error interno del servidor al procesar la dirección.")

# --- FIN NUEVO ENDPOINT ---


# --- Punto de Entrada (Opcional local, sin cambios) ---
# if __name__ == "__main__": import uvicorn; uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
# --- FIN main.py v2.4.0-mt ---
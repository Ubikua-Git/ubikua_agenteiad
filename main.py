from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests
import base64
import logging
import psycopg2
import psycopg2.extras
import tempfile
import re
import chardet
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(title="Asistente IA Ashotel API v2.3.5 (FTS + Correcciones)", version="2.3.5")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración ---
try:
    openai_api_key = os.getenv("OPENAI_API_KEY")
    assert openai_api_key, "Var OPENAI_API_KEY no encontrada."
    client = OpenAI(api_key=openai_api_key)
    logging.info("Cliente OpenAI OK.")
    
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Google API Keys no encontradas.")
    
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

# --- Modelos Pydantic ---
class PeticionConsulta(BaseModel):
    mensaje: str
    especializacion: str = "general"
    buscar_web: bool = False
    user_id: int | None = None

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

class ProcessRequest(BaseModel):
    doc_id: int
    user_id: int

class ProcessResponse(BaseModel):
    success: bool
    message: str | None = None
    error: str | None = None

# --- Prompts ---
BASE_PROMPT_CONSULTA = ("Eres el Asistente IA oficial de Ashotel...")
BASE_PROMPT_ANALISIS_DOC = ("Eres el Asistente IA oficial de Ashotel, experto en redactar informes...")
PROMPT_ESPECIALIZACIONES = {
    "general": "Actúa generalista.",
    "legal": "Enfoque legal.",
    "comunicacion": "Rol comunicación.",
    "formacion": "Especialista formación.",
    "informatica": "Aspectos técnicos.",
    "direccion": "Perspectiva estratégica.",
    "innovacion": "Enfoque novedad.",
    "contabilidad": "Experto contable.",
    "administracion": "Eficiencia procesos."
}
FRASES_BUSQUEDA = ["no tengo información", "no dispongo de información", "no tengo acceso", "no sé"]

# --- Temp Dir ---
TEMP_DIR = "/tmp/uploads_ashotel"
os.makedirs(TEMP_DIR, exist_ok=True)

# --- Funciones Auxiliares ---
def get_db_connection():
    if not DB_CONFIGURED:
        logging.error("Intento conexión BD fallido: Config incompleta.")
        return None
    try:
        return psycopg2.connect(
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            port=DB_PORT,
            connect_timeout=5
        )
    except (Exception, psycopg2.Error) as error:
        logging.error(f"Error conectar PGSQL {DB_HOST}:{DB_PORT}: {error}")
        return None

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    texto = ""
    logging.info(f"Extrayendo texto PDF/DOCX de: {ruta_archivo}")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo)
                if lector.is_encrypted:
                    logging.warning(f"PDF encriptado: {ruta_archivo}")
                for pagina in lector.pages:
                    texto_pagina = pagina.extract_text()
                    if texto_pagina:
                        texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]:
            doc = Document(ruta_archivo)
            texto = "\n".join([p.text for p in doc.paragraphs if p.text])
        else:
            return "[Error interno: Tipo no esperado en extraer_texto_pdf_docx]"
        logging.info(f"Texto extraído PDF/DOCX OK (longitud: {len(texto)}).")
        return texto.strip()
    except pdf_errors.PdfReadError as e:
        logging.error(f"Error leer PDF {ruta_archivo}: {e}")
        return f"[Error PDF: No se pudo leer]"
    except PackageNotFoundError:
        logging.error(f"Error DOCX {ruta_archivo}: No válido.")
        return "[Error DOCX: Archivo inválido]"
    except FileNotFoundError:
        logging.error(f"Error interno: {ruta_archivo} no encontrado.")
        return "[Error interno: Archivo no encontrado]"
    except Exception as e:
        logging.error(f"Error extraer PDF/DOCX {ruta_archivo}: {e}", exc_info=True)
        return "[Error interno procesando PDF/DOCX.]"

def extraer_texto_simple(ruta_archivo: str) -> str:
    logging.info(f"Extrayendo texto simple de: {ruta_archivo}")
    try:
        with open(ruta_archivo, 'rb') as fb:
            raw_data = fb.read()
            detected_encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f:
            texto = f.read()
        logging.info(f"Texto extraído simple OK (longitud: {len(texto)}).")
        return texto.strip()
    except FileNotFoundError:
        logging.error(f"Error interno: {ruta_archivo} no encontrado para simple.")
        return "[Error interno: Archivo no encontrado]"
    except Exception as e:
        logging.error(f"Error extraer texto simple {ruta_archivo}: {e}")
        return "[Error interno procesando texto plano.]"

# --- Endpoints de la API ---

# Endpoint para procesar texto de documentos subidos
@app.post("/process-document", response_model=ProcessResponse)
async def process_document_text(request: ProcessRequest):
    doc_id = request.doc_id
    current_user_id = request.user_id
    logging.info(f"Solicitud procesar doc ID: {doc_id} user: {current_user_id}")

    if not DB_CONFIGURED or not PHP_BRIDGE_CONFIGURED:
        return ProcessResponse(success=False, error="Configuración incompleta (BD o PHP Bridge).")

    conn = None
    original_fname = None
    temp_path = None
    try:
        # 1. Obtener info del documento
        conn = get_db_connection()
        assert conn, "No se pudo conectar a BD."
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
            cursor.execute(
                "SELECT original_filename, file_type, stored_path FROM user_documents WHERE id = %s AND user_id = %s",
                (doc_id, current_user_id)
            )
            doc_info = cursor.fetchone()
            assert doc_info, "Documento no encontrado o no pertenece al usuario."
            original_fname = doc_info['original_filename']
            file_type = doc_info['file_type']
            stored_path = doc_info['stored_path']

        # 2. Obtener contenido del archivo via PHP Bridge
        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&api_key={PHP_API_SECRET_KEY}"
        logging.info(f"Solicitando doc ID {doc_id} a PHP...")
        response = requests.get(serve_url, timeout=30, stream=True)
        response.raise_for_status()

        # 3. Guardar temporalmente y extraer texto
        file_ext = os.path.splitext(original_fname)[1].lower().strip('.')
        extracted_text = None
        if file_ext in ['pdf', 'doc', 'docx', 'txt', 'csv']:
            fd, temp_path = tempfile.mkstemp(suffix=f'.{file_ext}', dir=TEMP_DIR)
            logging.info(f"Guardando en temp: {temp_path}")
            try:
                with os.fdopen(fd, 'wb') as temp_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)
                # Seleccionar función de extracción
                if file_ext in ['pdf', 'doc', 'docx']:
                    extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
                elif file_ext in ['txt', 'csv']:
                    extracted_text = extraer_texto_simple(temp_path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logging.info(f"Temp file {temp_path} eliminado tras extracción.")
                    except OSError as e:
                        logging.error(f"Error borrar temp {temp_path}: {e}")
        else:
            extracted_text = "[Extracción no soportada para este tipo de archivo]"

        # 4. Actualizar Base de Datos (Trigger actualizará fts_vector)
        if extracted_text is None:
            raise ValueError("Fallo la extracción de texto.")
        logging.info(f"Actualizando BD doc ID {doc_id} con texto (longitud: {len(extracted_text)})...")
        with conn.cursor() as cursor:
            sql_update = "UPDATE user_documents SET extracted_text = %s WHERE id = %s AND user_id = %s"
            cursor.execute(sql_update, (extracted_text, doc_id, current_user_id))
            if cursor.rowcount == 0:
                logging.warning(f"UPDATE texto no afectó filas doc {doc_id}")
        conn.commit()
        logging.info(f"BD actualizada doc ID {doc_id}.")
        return ProcessResponse(success=True, message="Documento procesado.")

    except AssertionError as e:
        logging.error(f"Assertion error procesando doc {doc_id}: {e}")
        return ProcessResponse(success=False, error=str(e))
    except requests.exceptions.RequestException as e:
        logging.error(f"Error solicitar PHP doc {doc_id}: {e}")
        return ProcessResponse(success=False, error=f"Error conexión PHP: {e}")
    except (Exception, psycopg2.Error) as e:
        logging.error(f"Error procesando doc {doc_id}: {e}", exc_info=True)
        if conn and not conn.closed:
            try:
                conn.rollback()
            except Exception:
                pass
        return ProcessResponse(success=False, error=f"Error interno: {e}")
    finally:
        if conn and not conn.closed:
            conn.close()

# --- Endpoint de consulta ---
@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    if not client:
        raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    especializacion = datos.especializacion.lower()
    mensaje_usuario = datos.mensaje
    forzar_busqueda_web = datos.buscar_web
    current_user_id = datos.user_id
    logging.info(f"Consulta: User={current_user_id}, Espec='{especializacion}', Web={forzar_busqueda_web}")

    custom_prompt_text = ""
    if current_user_id and DB_CONFIGURED:
        conn_prompt = get_db_connection()
        if conn_prompt:
            try:
                with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s", (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'):
                        custom_prompt_text = result['custom_prompt'].strip()
            except (Exception, psycopg2.Error) as e:
                logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                if conn_prompt:
                    conn_prompt.close()

    document_context = ""
    if current_user_id and DB_CONFIGURED:
        conn_docs = get_db_connection()
        if conn_docs:
            relevant_docs_texts = []
            try:
                with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    search_query = mensaje_usuario
                    sql_fts = """
                        SELECT original_filename, extracted_text,
                               ts_rank(fts_vector, plainto_tsquery('spanish', %s)) as relevance
                        FROM user_documents
                        WHERE user_id = %s
                          AND is_active_for_ai = TRUE
                          AND fts_vector @@ plainto_tsquery('spanish', %s)
                          AND extracted_text IS NOT NULL AND extracted_text != '' AND NOT extracted_text LIKE '[Error%'
                        ORDER BY relevance DESC
                        LIMIT 3
                    """
                    cursor.execute(sql_fts, (search_query, current_user_id, search_query))
                    relevant_docs = cursor.fetchall()

                    if relevant_docs:
                        context_parts = ["\n\n### Contexto Relevante de Documentos del Usuario ###"]
                        current_token_count = 0
                        for doc in relevant_docs:
                            filename = doc['original_filename']
                            text = doc['extracted_text']
                            doc_tokens = len(text.split()) * 1.3
                            if current_token_count + doc_tokens < 3500:
                                context_parts.append(f"\n--- Documento: {filename} ---")
                                context_parts.append(text[:3500 - current_token_count])  # Limitar a 3500 tokens
                                current_token_count += doc_tokens
                        document_context = "\n".join(context_parts)
            except (Exception, psycopg2.Error) as e:
                logging.error(f"Error BD FTS user {current_user_id}: {e}")
            finally:
                if conn_docs:
                    conn_docs.close()

    system_prompt = "\n".join([BASE_PROMPT_CONSULTA, PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"]), custom_prompt_text, document_context])
    logging.debug(f"Prompt para OpenAI: {system_prompt[:500]}")

    try:
        logging.info("Llamada OpenAI...")
        respuesta_inicial = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": mensaje_usuario}],
            temperature=0.5,
            max_tokens=1500
        )

        texto_respuesta_final = respuesta_inicial.choices[0].message.content.strip()

        if any(frase in texto_respuesta_final.lower() for frase in FRASES_BUSQUEDA) and forzar_busqueda_web:
            web_resultados_html = buscar_google(mensaje_usuario)
            texto_respuesta_final += "\n\n" + web_resultados_html

    except APIError as e:
        logging.error(f"Error OpenAI /consulta: {e}")
        raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except Exception as e:
        logging.error(f"Error inesperado /consulta: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno.")

    return RespuestaConsulta(respuesta=texto_respuesta_final)
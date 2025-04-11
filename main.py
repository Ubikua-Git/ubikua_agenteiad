# --- INICIO main.py v2.3.0 (PostgreSQL + FTS Context) ---
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
import chardet # Para detectar encoding de texto
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try: from bs4 import BeautifulSoup; BS4_AVAILABLE = True
except ImportError: BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI( title="Asistente IA Ashotel API v2.3.0 (FTS Context)", version="2.3.0" )
app.add_middleware( CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], )

# --- Configuración (Claves, BD, PHP Bridge) ---
try:
    openai_api_key = os.getenv("OPENAI_API_KEY"); assert openai_api_key, "OPENAI_API_KEY no encontrada."
    client = OpenAI(api_key=openai_api_key); logging.info("Cliente OpenAI OK.")
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY"); GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX: logging.warning("Google API Keys no encontradas.")
    DB_HOST = os.getenv("DB_HOST"); DB_USER = os.getenv("DB_USER"); DB_PASS = os.getenv("DB_PASS"); DB_NAME = os.getenv("DB_NAME"); DB_PORT = int(os.getenv("DB_PORT", 5432))
    if not all([DB_HOST, DB_USER, DB_PASS, DB_NAME]): logging.warning("Faltan variables DB."); DB_CONFIGURED = False
    else: DB_CONFIGURED = True; logging.info("Credenciales BD PostgreSQL OK.")
    PHP_FILE_SERVE_URL = os.getenv("PHP_FILE_SERVE_URL"); PHP_API_SECRET_KEY = os.getenv("PHP_API_SECRET_KEY")
    if not PHP_FILE_SERVE_URL or not PHP_API_SECRET_KEY: logging.warning("Faltan PHP_FILE_SERVE_URL o PHP_API_SECRET_KEY."); PHP_BRIDGE_CONFIGURED = False
    else: PHP_BRIDGE_CONFIGURED = True; logging.info("Config PHP Bridge OK.")
except Exception as e: logging.error(f"Error Configuración Crítica: {e}", exc_info=True); client = None; DB_CONFIGURED = False; PHP_BRIDGE_CONFIGURED = False

# --- Modelos Pydantic ---
class PeticionConsulta(BaseModel): mensaje: str; especializacion: str = "general"; buscar_web: bool = False; user_id: int | None = None
class RespuestaConsulta(BaseModel): respuesta: str
class RespuestaAnalisis(BaseModel): informe: str
class ProcessRequest(BaseModel): doc_id: int; user_id: int # Para nuevo endpoint
class ProcessResponse(BaseModel): success: bool; message: str | None = None; error: str | None = None

# --- Prompts ---
BASE_PROMPT_CONSULTA = ("Eres el Asistente IA oficial de Ashotel...") # Prompt completo
BASE_PROMPT_ANALISIS_DOC = ("Eres el Asistente IA oficial de Ashotel, experto en redactar informes...") # Prompt completo
PROMPT_ESPECIALIZACIONES = { "general": "Actúa generalista.", "legal": "Enfoque legal.", "comunicacion": "Rol comunicación.", "formacion": "Especialista formación.", "informatica": "Aspectos técnicos.", "direccion": "Perspectiva estratégica.", "innovacion": "Enfoque novedad.", "contabilidad": "Experto contable.", "administracion": "Eficiencia procesos." }
FRASES_BUSQUEDA = ["no tengo información", "no dispongo de información", "no tengo acceso", "no sé"]

# --- Temp Dir ---
TEMP_DIR = "/tmp/uploads_ashotel"; os.makedirs(TEMP_DIR, exist_ok=True)

# --- Funciones Auxiliares ---
def get_db_connection(): # Conexión PostgreSQL
    if not DB_CONFIGURED: return None
    try: return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT, connect_timeout=5)
    except (Exception, psycopg2.Error) as error: logging.error(f"Error conectar PGSQL {DB_HOST}:{DB_PORT}: {error}"); return None

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str: # Extracción PDF/DOCX
    # ... (Código completo igual que antes) ...
    pass 
def extraer_texto_simple(ruta_archivo: str) -> str: # Extracción TXT/CSV
    try:
        with open(ruta_archivo, 'rb') as fb: raw_data = fb.read(); detected_encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f: texto = f.read()
        logging.info(f"Texto extraído simple (longitud: {len(texto)}).")
        return texto.strip()
    except Exception as e: logging.error(f"Error extraer texto simple {ruta_archivo}: {e}"); return "[Error interno procesando texto plano.]"

def buscar_google(query: str) -> str: # Búsqueda Google
    # ... (Código completo igual que antes) ...
    pass 

# --- Endpoints de la API ---

# NUEVO Endpoint para procesar texto de documentos subidos
@app.post("/process-document", response_model=ProcessResponse)
async def process_document_text(request: ProcessRequest):
    doc_id = request.doc_id
    current_user_id = request.user_id
    logging.info(f"Solicitud para procesar texto del doc ID: {doc_id} para user: {current_user_id}")

    if not DB_CONFIGURED or not PHP_BRIDGE_CONFIGURED: return ProcessResponse(success=False, error="Configuración incompleta (BD o PHP Bridge).")

    conn = None; original_fname = None; file_type = None; stored_path = None; extracted_text = None; temp_path = None

    try:
        # 1. Obtener info del documento y verificar propiedad
        conn = get_db_connection(); assert conn, "No se pudo conectar a la BD."
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
             cursor.execute("SELECT original_filename, file_type, stored_path FROM user_documents WHERE id = %s AND user_id = %s", (doc_id, current_user_id))
             doc_info = cursor.fetchone(); assert doc_info, "Documento no encontrado o no pertenece al usuario."
             original_fname = doc_info['original_filename']; file_type = doc_info['file_type']; stored_path = doc_info['stored_path']

        # 2. Obtener contenido del archivo via PHP Bridge
        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&api_key={PHP_API_SECRET_KEY}"
        logging.info(f"Solicitando contenido doc ID {doc_id} a PHP...")
        response = requests.get(serve_url, timeout=30, stream=True); response.raise_for_status()

        # 3. Guardar temporalmente y extraer texto
        file_ext = os.path.splitext(original_fname)[1].lower().strip('.')
        if file_ext in ['pdf', 'doc', 'docx', 'txt', 'csv']:
            fd, temp_path = tempfile.mkstemp(suffix=f'.{file_ext}', dir=TEMP_DIR); logging.info(f"Guardando en temp: {temp_path}")
            try:
                with os.fdopen(fd, 'wb') as temp_file:
                    for chunk in response.iter_content(chunk_size=8192): temp_file.write(chunk)
                # Extraer texto según extensión
                if file_ext in ['pdf', 'doc', 'docx']: extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
                elif file_ext in ['txt', 'csv']: extracted_text = extraer_texto_simple(temp_path)
            finally:
                if os.path.exists(temp_path): try: os.remove(temp_path) except OSError as e: logging.error(f"Error borrar temp {temp_path}: {e}")
        else: extracted_text = "[Extracción no soportada]"

        # 4. Actualizar Base de Datos (Trigger actualizará fts_vector)
        if extracted_text is None: raise ValueError("Fallo la extracción de texto.")
        logging.info(f"Actualizando BD doc ID {doc_id} con texto extraído (longitud: {len(extracted_text)})...")
        with conn.cursor() as cursor:
             sql_update = "UPDATE user_documents SET extracted_text = %s WHERE id = %s AND user_id = %s"
             cursor.execute(sql_update, (extracted_text, doc_id, current_user_id))
             if cursor.rowcount == 0: logging.warning(f"UPDATE de texto no afectó filas para doc {doc_id}, user {current_user_id}")
        conn.commit(); logging.info(f"BD actualizada para doc ID {doc_id}.")
        return ProcessResponse(success=True, message="Documento procesado.")

    except AssertionError as e: logging.error(f"Assertion error procesando doc {doc_id}: {e}"); return ProcessResponse(success=False, error=str(e))
    except requests.exceptions.RequestException as e: logging.error(f"Error solicitando archivo PHP doc {doc_id}: {e}"); return ProcessResponse(success=False, error=f"Error conexión PHP: {e}")
    except (Exception, psycopg2.Error) as e: logging.error(f"Error procesando doc {doc_id}: {e}", exc_info=True); if conn and not conn.closed: conn.rollback()
    return ProcessResponse(success=False, error=f"Error interno: {e}")
    finally:
        if conn and not conn.closed: conn.close()


@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    especializacion = datos.especializacion.lower(); mensaje_usuario = datos.mensaje; forzar_busqueda_web = datos.buscar_web; current_user_id = datos.user_id
    logging.info(f"Consulta: User={current_user_id}, Espec='{especializacion}', Web={forzar_busqueda_web}")

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""; conn_prompt = None
    if current_user_id and DB_CONFIGURED: # ... (Lógica igual para obtener custom_prompt_text) ...
        pass

    # --- OBTENER CONTEXTO DOCUMENTOS CON FTS (NUEVO) ---
    document_context = ""; conn_docs = None; MAX_CONTEXT_TOKENS = 3500 # Límite tokens aprox. para contexto
    if current_user_id and DB_CONFIGURED:
        conn_docs = get_db_connection()
        if conn_docs:
            try:
                with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    # Preparar consulta FTS
                    # Usamos plainto_tsquery para convertir el texto del usuario en términos de búsqueda
                    # y @@ para buscar en el índice fts_vector. 'spanish' es el idioma.
                    # Ordenamos por relevancia usando ts_rank y limitamos a los 2 más relevantes.
                    sql_fts = """
                        SELECT original_filename, extracted_text, 
                               ts_rank(fts_vector, plainto_tsquery('spanish', %s)) as relevance
                        FROM user_documents
                        WHERE user_id = %s 
                          AND is_active_for_ai = TRUE 
                          AND fts_vector @@ plainto_tsquery('spanish', %s)
                        ORDER BY relevance DESC
                        LIMIT 2 
                    """
                    cursor.execute(sql_fts, (mensaje_usuario, current_user_id, mensaje_usuario))
                    relevant_docs = cursor.fetchall()
                    logging.info(f"FTS encontró {len(relevant_docs)} documentos relevantes para user {current_user_id}.")

                    # Construir el contexto con los documentos encontrados
                    if relevant_docs:
                        context_parts = ["\n\n### Contexto Relevante de Documentos del Usuario ###"]
                        current_token_count = 0 # Estimación simple (contar palabras o usar tiktoken si se instala)
                        for doc in relevant_docs:
                            filename = doc['original_filename']
                            text = doc['extracted_text'] if doc['extracted_text'] else "[Contenido no extraído]"
                            # Estimación simple de tokens (1 palabra ~ 1.3 tokens)
                            doc_tokens = len(text.split()) * 1.3 
                            
                            # Añadir si cabe en el límite de contexto
                            if current_token_count + doc_tokens < MAX_CONTEXT_TOKENS:
                                context_parts.append(f"\n--- Documento: {filename} ---")
                                context_parts.append(text)
                                current_token_count += doc_tokens
                                logging.info(f"Añadido contexto de '{filename}'. Tokens acumulados aprox: {current_token_count}")
                            else:
                                logging.warning(f"Documento '{filename}' omitido por límite de tokens de contexto.")
                                break # No añadir más si se supera el límite
                        
                        if len(context_parts) > 1: # Si se añadió algún documento
                             document_context = "\n".join(context_parts)

            except (Exception, psycopg2.Error) as e: logging.error(f"Error BD FTS user {current_user_id}: {e}")
            finally:
                if conn_docs: conn_docs.close()

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico]
    if custom_prompt_text: system_prompt_parts.extend(["\n\n### Instrucciones Adicionales Usuario ###", custom_prompt_text])
    if document_context: system_prompt_parts.append(document_context) # Añadir contexto FTS
    system_prompt = "\n".join(system_prompt_parts); logging.debug(f"System Prompt Final (primeros 500): {system_prompt[:500]}")

    # --- Lógica OpenAI / Búsqueda Web ---
    # ... (igual que antes, usa el system_prompt final con contexto FTS) ...

    return RespuestaConsulta(respuesta=texto_respuesta_final)


# Endpoint analizar_documento (Sin cambios mayores, solo usa prompt personalizado)
@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento( file: UploadFile = File(...), especializacion: str = Form("general"), user_id: int | None = Form(None) ):
     # ... (Código completo igual que v2.2.1, asegurándose de que busca y añade el custom_prompt_text) ...
    pass # Asegúrate de tener aquí el código completo de la v2.2.1

# --- Punto de Entrada (Opcional) ---
# if __name__ == "__main__": import uvicorn; uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# --- FIN main.py v2.3.0 ---
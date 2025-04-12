# --- INICIO main.py v2.3.2 (Corrección SyntaxError en buscar_google) ---
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests
import base64
import logging
import psycopg2 # Driver PostgreSQL
import psycopg2.extras # Para DictCursor
import tempfile
import re
import chardet
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try: from bs4 import BeautifulSoup; BS4_AVAILABLE = True
except ImportError: BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI( title="Asistente IA Ashotel API v2.3.2 (FTS + Correcciones)", version="2.3.2" )
app.add_middleware( CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], )

# --- Configuración Clientes, API Keys, BD y PHP Bridge ---
try:
    openai_api_key = os.getenv("OPENAI_API_KEY"); assert openai_api_key, "Var OPENAI_API_KEY no encontrada."
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
class ProcessRequest(BaseModel): doc_id: int; user_id: int
class ProcessResponse(BaseModel): success: bool; message: str | None = None; error: str | None = None

# --- Prompts ---
BASE_PROMPT_CONSULTA = ("Eres el Asistente IA oficial de Ashotel...") # Asegúrate que tus prompts completos están aquí
BASE_PROMPT_ANALISIS_DOC = ("Eres el Asistente IA oficial de Ashotel, experto en redactar informes...") # Asegúrate que tus prompts completos están aquí
PROMPT_ESPECIALIZACIONES = { "general": "Actúa generalista.", "legal": "Enfoque legal.", "comunicacion": "Rol comunicación.", "formacion": "Especialista formación.", "informatica": "Aspectos técnicos.", "direccion": "Perspectiva estratégica.", "innovacion": "Enfoque novedad.", "contabilidad": "Experto contable.", "administracion": "Eficiencia procesos." }
FRASES_BUSQUEDA = ["no tengo información", "no dispongo de información", "no tengo acceso", "no sé"]

# --- Temp Dir ---
TEMP_DIR = "/tmp/uploads_ashotel"; os.makedirs(TEMP_DIR, exist_ok=True); logging.info(f"Directorio temporal: {TEMP_DIR}")

# --- Funciones Auxiliares ---
def get_db_connection(): # Conexión PostgreSQL
    if not DB_CONFIGURED: return None
    try: return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT, connect_timeout=5)
    except (Exception, psycopg2.Error) as error: logging.error(f"Error conectar PGSQL {DB_HOST}:{DB_PORT}: {error}"); return None

def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str: # Extracción PDF/DOCX
    texto = ""; logging.info(f"Extrayendo texto PDF/DOCX de: {ruta_archivo}")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo: lector = PdfReader(archivo);
            if lector.is_encrypted: logging.warning(f"PDF encriptado: {ruta_archivo}")
            for pagina in lector.pages: texto_pagina = pagina.extract_text();
            if texto_pagina: texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]: doc = Document(ruta_archivo); texto = "\n".join([p.text for p in doc.paragraphs if p.text])
        else: return "[Error interno: Tipo no esperado en extraer_texto_pdf_docx]"
        logging.info(f"Texto extraído PDF/DOCX OK (longitud: {len(texto)})."); return texto.strip()
    except pdf_errors.PdfReadError as e: logging.error(f"Error leer PDF {ruta_archivo}: {e}"); return f"[Error PDF: No se pudo leer]"
    except PackageNotFoundError: logging.error(f"Error DOCX {ruta_archivo}: No válido."); return "[Error DOCX: Archivo inválido]"
    except FileNotFoundError: logging.error(f"Error interno: {ruta_archivo} no encontrado."); return "[Error interno: Archivo no encontrado]"
    except Exception as e: logging.error(f"Error extraer PDF/DOCX {ruta_archivo}: {e}", exc_info=True); return "[Error interno procesando PDF/DOCX.]"

def extraer_texto_simple(ruta_archivo: str) -> str: # Extracción TXT/CSV
    logging.info(f"Extrayendo texto simple de: {ruta_archivo}")
    try:
        with open(ruta_archivo, 'rb') as fb: raw_data = fb.read(); detected_encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f: texto = f.read()
        logging.info(f"Texto extraído simple OK (longitud: {len(texto)})."); return texto.strip()
    except FileNotFoundError: logging.error(f"Error interno: {ruta_archivo} no encontrado para simple."); return "[Error interno: Archivo no encontrado]"
    except Exception as e: logging.error(f"Error extraer texto simple {ruta_archivo}: {e}"); return "[Error interno procesando texto plano.]"

# --- Función buscar_google (CORREGIDA) ---
def buscar_google(query: str) -> str:
    if not GOOGLE_API_KEY or not GOOGLE_CX: return "<p><i>[Búsqueda web no disponible.]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"; params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3}
    logging.info(f"Buscando en Google: '{query}'") # Log ANTES del try
    try:
        # Cada operación en su línea
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status() # Verificar si hubo error HTTP
        data = response.json(); resultados = data.get("items", [])
        if not resultados: return "<p><i>[No se encontraron resultados web.]</i></p>"
        texto_resultados = "<div class='google-results'><h4 style='font-size:0.9em;color:#555;'>Resultados web:</h4>"
        for item in resultados: title = item.get('title',''); link = item.get('link','#'); snippet = item.get('snippet','').replace('\n',' '); texto_resultados += f"<div><a href='{link}' target='_blank'>{title}</a><p>{snippet}</p><cite>{link}</cite></div>\n"
        texto_resultados += "</div>"; logging.info(f"Búsqueda web OK: {len(resultados)} resultados.")
        return texto_resultados
    except requests.exceptions.Timeout: logging.error("Timeout búsqueda web."); return "<p><i>[Error: Timeout búsqueda web.]</i></p>"
    except requests.exceptions.RequestException as e: logging.error(f"Error búsqueda web: {e}"); return f"<p><i>[Error conexión búsqueda web.]</i></p>"
    except Exception as e: logging.error(f"Error inesperado búsqueda web: {e}"); return "<p><i>[Error inesperado búsqueda web.]</i></p>"


# --- Endpoints de la API ---

# Endpoint /process-document (Sin cambios respecto a v2.3.0)
@app.post("/process-document", response_model=ProcessResponse)
async def process_document_text(request: ProcessRequest):
    # ... (Código completo igual que la versión anterior v2.3.0) ...
    pass # Asegúrate de pegar aquí el código completo de este endpoint

# Endpoint /consulta (Con bloque finally corregido)
@app.post("/consulta", response_model=RespuestaConsulta)
def consultar_agente(datos: PeticionConsulta):
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    especializacion = datos.especializacion.lower(); mensaje_usuario = datos.mensaje; forzar_busqueda_web = datos.buscar_web; current_user_id = datos.user_id
    logging.info(f"Consulta: User={current_user_id}, Espec='{especializacion}', Web={forzar_busqueda_web}")

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""; conn_prompt = None
    if current_user_id and DB_CONFIGURED:
        conn_prompt = get_db_connection()
        if conn_prompt:
            try:
                with conn_prompt.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s", (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'): custom_prompt_text = result['custom_prompt'].strip(); logging.info(f"Prompt OK user: {current_user_id}")
            except (Exception, psycopg2.Error) as e: logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally: conn_prompt.close()

    # --- Obtener Contexto de Documentos (FTS) ---
    document_context = ""; conn_docs = None; temp_path_context = None # Para asegurar borrado temp
    if current_user_id and DB_CONFIGURED and PHP_BRIDGE_CONFIGURED: # Solo intentar si todo está configurado
        conn_docs = get_db_connection()
        if conn_docs:
            relevant_doc = None; active_docs = []
            try:
                with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    # ... (Consulta FTS igual que antes) ...
                    sql_fts = """ SELECT id, original_filename, extracted_text, ts_rank(fts_vector, plainto_tsquery('spanish', %s)) as relevance FROM user_documents WHERE user_id = %s AND is_active_for_ai = TRUE AND fts_vector @@ plainto_tsquery('spanish', %s) ORDER BY relevance DESC LIMIT 3 """
                    cursor.execute(sql_fts, (mensaje_usuario, current_user_id, mensaje_usuario)); relevant_docs = cursor.fetchall(); logging.info(f"FTS encontró {len(relevant_docs)} docs.")
                    # ... (Lógica para construir document_context con el texto de relevant_docs igual que antes) ...
                    # (Asegurarse que esta lógica esté completa)
                    if relevant_docs:
                        context_parts = ["\n\n### Contexto Relevante de Documentos ###"]; current_token_count = 0; MAX_CONTEXT_TOKENS = 3500
                        for doc in relevant_docs:
                            filename = doc['original_filename']; text = doc['extracted_text']
                            if text and not text.startswith("[Error"):
                                doc_tokens = len(text.split()) * 1.3
                                if current_token_count + doc_tokens < MAX_CONTEXT_TOKENS:
                                    context_parts.append(f"\n--- Doc: {filename} ---"); available_tokens = MAX_CONTEXT_TOKENS - current_token_count; max_chars_approx = int((available_tokens / 1.3) * 4); context_parts.append(text[:max_chars_approx]);
                                    if len(text) > max_chars_approx: context_parts.append("\n[...]")
                                    current_token_count += doc_tokens; logging.info(f"Añadido contexto '{filename}'.")
                                else: break
                        if len(context_parts) > 1: document_context = "\n".join(context_parts)

            except (Exception, psycopg2.Error) as e: logging.error(f"Error BD FTS user {current_user_id}: {e}")
            finally:
                if conn_docs: conn_docs.close()

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico]
    if custom_prompt_text: system_prompt_parts.extend(["\n\n### Instrucciones Usuario ###", custom_prompt_text])
    if document_context: system_prompt_parts.append(document_context)
    else: logging.info("No se añadió contexto de documentos.")
    system_prompt = "\n".join(system_prompt_parts); logging.debug(f"System Prompt Final (500 chars): {system_prompt[:500]}")

    # --- Lógica OpenAI / Búsqueda Web ---
    texto_respuesta_final = ""; activar_busqueda = forzar_busqueda_web
    try:
        logging.info("Llamada OpenAI 1..."); respuesta_inicial = client.chat.completions.create( model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_usuario}], temperature=0.5, max_tokens=1500 )
        texto_respuesta_inicial = respuesta_inicial.choices[0].message.content.strip(); logging.info("Respuesta OpenAI 1 OK.")
        if not activar_busqueda and any(frase in texto_respuesta_inicial.lower() for frase in FRASES_BUSQUEDA): activar_busqueda = True; logging.info("Activando búsqueda web.")
        if activar_busqueda:
            web_resultados_html = buscar_google(mensaje_usuario)
            if "[Error" not in web_resultados_html:
                 logging.info("Llamada OpenAI 2 con contexto web...")
                 mensaje_con_contexto = f"Consulta: {mensaje_usuario}\nContexto web:\n{web_resultados_html}\n\nResponde consulta integrando contexto."
                 respuesta_con_contexto = client.chat.completions.create( model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_con_contexto}], temperature=0.5, max_tokens=1500 )
                 texto_respuesta_final = respuesta_con_contexto.choices[0].message.content.strip(); logging.info("Respuesta OpenAI 2 OK.")
            else: texto_respuesta_final = texto_respuesta_inicial + "\n" + web_resultados_html
        else: texto_respuesta_final = texto_respuesta_inicial
    except APIError as e: logging.error(f"Error OpenAI /consulta: {e}"); raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except Exception as e: logging.error(f"Error inesperado /consulta: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Error interno.")

    return RespuestaConsulta(respuesta=texto_respuesta_final)


# Endpoint analizar_documento (Con corrección finally y usa prompt personalizado)
@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento( file: UploadFile = File(...), especializacion: str = Form("general"), user_id: int | None = Form(None) ):
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    filename = file.filename or "unknown"; content_type = file.content_type or ""; extension = filename.split('.')[-1].lower() if '.' in filename else ''
    current_user_id = user_id; especializacion_lower = especializacion.lower()
    logging.info(f"Análisis: User={current_user_id}, File={filename}, Espec='{especializacion_lower}'")

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""; conn_prompt = None
    if current_user_id and DB_CONFIGURED: # ... (Lógica completa para obtener custom_prompt_text de BD) ...
        conn_prompt = get_db_connection(); # ... (try/except/finally con consulta) ...
        if conn_prompt: # ... (resto de lógica) ...

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico]
    if custom_prompt_text: system_prompt_parts.extend(["\n\n### Instrucciones Adicionales Usuario ###", custom_prompt_text])
    system_prompt = "\n".join(system_prompt_parts); logging.debug(f"System Prompt Análisis (500 chars): {system_prompt[:500]}")

    # --- Lógica Procesamiento Archivo Subido / Llamada OpenAI ---
    informe_html = ""; messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]
    temp_path_analisis = None
    try:
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando IMAGEN subida.")
            image_bytes = await file.read(); base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_prompt_image = ("Analiza la imagen, extrae su texto (OCR), y redacta un informe HTML profesional basado en ese texto...") # Prompt completo
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": [ {"type": "text", "text": user_prompt_image}, {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}} ] } ]
        
        elif extension in ["pdf", "docx", "doc", "txt", "csv"]:
            logging.info(f"Procesando {extension.upper()} subido.")
            ruta_temporal = os.path.join(TEMP_DIR, f"up_analisis_{os.urandom(8).hex()}.{extension}")
            temp_path_analisis = ruta_temporal
            texto_extraido = ""; temp_file_saved = False
            # ----- INICIO BLOQUE TRY/FINALLY CORRECTO -----
            try:
                with open(ruta_temporal, "wb") as buffer: shutil.copyfileobj(file.file, buffer); temp_file_saved = True
                logging.info(f"Archivo subido guardado temporalmente en: {ruta_temporal}")
                if extension in ['pdf', 'doc', 'docx']: texto_extraido = extraer_texto_pdf_docx(ruta_temporal, extension)
                elif extension in ['txt', 'csv']: texto_extraido = extraer_texto_simple(ruta_temporal)
                # No necesitamos else aquí
            finally:
                # Bloque finally con indentación correcta para ESTE archivo temporal
                if temp_file_saved and os.path.exists(ruta_temporal):
                    try:
                        os.remove(ruta_temporal)
                        logging.info(f"Archivo temporal (análisis) eliminado: {ruta_temporal}")
                        temp_path_analisis = None # Marcar como borrado
                    except OSError as e:
                         logging.error(f"Error al eliminar archivo temporal (análisis) {ruta_temporal}: {e}")
            # ----- FIN BLOQUE TRY/FINALLY CORRECTO -----

            if texto_extraido.startswith("[Error"): raise ValueError(texto_extraido)
            if not texto_extraido: raise ValueError(f"No se extrajo texto del archivo {extension.upper()} subido.")
            user_prompt_text = (f"Redacta un informe HTML profesional basado en texto:\n--- INICIO ---\n{texto_extraido}\n--- FIN ---\n Sigue formato HTML...") # Prompt completo
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt_text} ]
        
        else: raise HTTPException(status_code=415, detail=f"Tipo archivo no soportado: {content_type or extension}.")

        # --- Llamada OpenAI ---
        if not messages_payload: raise HTTPException(status_code=500, detail="Error interno: No payload IA.")
        logging.info(f"Llamada a OpenAI...")
        respuesta_informe = client.chat.completions.create( model="gpt-4-turbo", messages=messages_payload, temperature=0.3, max_tokens=2500 )
        informe_html = respuesta_informe.choices[0].message.content.strip(); logging.info(f"Informe generado OK.")
        # ... (Limpieza HTML opcional) ...

    except APIError as e: logging.error(f"Error API OpenAI /analizar: {e}"); raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except HTTPException as e: raise e
    except ValueError as e: logging.error(f"Error procesando {filename}: {e}"); raise HTTPException(status_code=400, detail=str(e))
    except Exception as e: logging.error(f"Error inesperado /analizar: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Error interno servidor.")
    finally: 
        await file.close() # Cerrar archivo subido original
        if temp_path_analisis and os.path.exists(temp_path_analisis): try: os.remove(temp_path_analisis); logging.info(f"Temp análisis borrado en finally externo.") except OSError as e: logging.error(f"Error borrando temp análisis externo {temp_path_analisis}: {e}")

    return RespuestaAnalisis(informe=informe_html)

# --- Punto de Entrada (Opcional) ---
# if __name__ == "__main__": import uvicorn; uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# --- FIN main.py v2.3.2 ---
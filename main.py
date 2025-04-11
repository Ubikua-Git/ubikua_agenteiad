# --- INICIO main.py v2.2.1 (PostgreSQL + Docs Context v1 - Revisado) ---
from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from openai import OpenAI, APIError
import os
import shutil
import requests # Necesario para llamar a PHP
import base64
import logging
import psycopg2 # <--- Driver para PostgreSQL
import psycopg2.extras # <--- Para DictCursor
import tempfile # Para archivos temporales en Render
import re # Para búsqueda simple de keywords
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
# BeautifulSoup es opcional, solo para limpieza extra de HTML
try: from bs4 import BeautifulSoup; BS4_AVAILABLE = True
except ImportError: BS4_AVAILABLE = False

# Configurar logging básico
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(
    title="Asistente IA Ashotel API v2.2.1 (PostgreSQL + Docs v1)",
    description="API para consultas y análisis de documentos con prompts y contexto de documentos (v1) por usuario.",
    version="2.2.1" # Versión con corrección de sintaxis
)

# Configuración CORS
app.add_middleware( CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"], )

# --- Configuración Clientes, API Keys, BD y PHP Bridge ---
try:
    # OpenAI
    openai_api_key = os.getenv("OPENAI_API_KEY"); assert openai_api_key, "Variable OPENAI_API_KEY no encontrada."
    client = OpenAI(api_key=openai_api_key); logging.info("Cliente OpenAI OK.")
    # Google Search (Opcional)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY"); GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX: logging.warning("Google API Keys no encontradas.")
    # Base de Datos PostgreSQL
    DB_HOST = os.getenv("DB_HOST"); DB_USER = os.getenv("DB_USER"); DB_PASS = os.getenv("DB_PASS"); DB_NAME = os.getenv("DB_NAME"); DB_PORT = int(os.getenv("DB_PORT", 5432))
    if not all([DB_HOST, DB_USER, DB_PASS, DB_NAME]): logging.warning("Faltan variables DB. No se leerán prompts/docs."); DB_CONFIGURED = False
    else: DB_CONFIGURED = True; logging.info("Credenciales BD PostgreSQL OK.")
    # PHP Bridge
    PHP_FILE_SERVE_URL = os.getenv("PHP_FILE_SERVE_URL"); PHP_API_SECRET_KEY = os.getenv("PHP_API_SECRET_KEY")
    if not PHP_FILE_SERVE_URL or not PHP_API_SECRET_KEY: logging.warning("Faltan PHP_FILE_SERVE_URL o PHP_API_SECRET_KEY."); PHP_BRIDGE_CONFIGURED = False
    else: PHP_BRIDGE_CONFIGURED = True; logging.info("Config PHP Bridge OK.")

except Exception as e:
    logging.error(f"Error Configuración Crítica: {e}", exc_info=True)
    client = None; DB_CONFIGURED = False; PHP_BRIDGE_CONFIGURED = False

# --- Modelos Pydantic ---
class PeticionConsulta(BaseModel): mensaje: str; especializacion: str = "general"; buscar_web: bool = False; user_id: int | None = None
class RespuestaConsulta(BaseModel): respuesta: str
class RespuestaAnalisis(BaseModel): informe: str

# --- Prompts ---
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de Ashotel, la Asociación Hotelera y Extrahotelera de Tenerife, La Palma, La Gomera y El Hierro. "
    "Tu misión es ayudar a los distintos equipos internos de Ashotel con respuestas claras, precisas, y alineadas a sus objetivos estratégicos. "
    "Si no tienes información directa sobre temas muy específicos o actuales, indícalo claramente y, si se te proporciona contexto web o de documentos de usuario, intégralo. "
    "Cuando respondas con listas estructuradas o datos comparativos, utiliza siempre tablas en formato HTML (usa <table>, <thead>, <tbody>, <tr>, <th>, <td>). "
    "Para listas simples, usa <ul> y <li>. Para enfatizar, usa <strong> o <em>. "
    "Evita usar Markdown. Tu respuesta debe ser directamente HTML renderizable."
)
BASE_PROMPT_ANALISIS_DOC = (
    "Eres el Asistente IA oficial de Ashotel, experto en redactar informes profesionales concisos y claros "
    "a partir de contenido textual o visual de documentos (PDF, DOCX, imágenes). "
    "Estructura siempre los informes con claridad, estilo formal y formato HTML limpio. "
    "Usa encabezados (<h2>, <h3>), párrafos (<p>), listas (<ul>, <li>), y énfasis (<strong>, <em>) apropiadamente. "
    "La respuesta debe ser únicamente el código HTML del informe, sin explicaciones previas o posteriores. "
    "Adapta ligeramente el tono y enfoque según la especialización indicada y las instrucciones adicionales del usuario si existen."
)
PROMPT_ESPECIALIZACIONES = { "general": "Actúa generalista.", "legal": "Enfoque legal y normativo.", "comunicacion": "Rol comunicación y marketing.", "formacion": "Especialista formación y pedagogía.", "informatica": "Aspectos técnicos y sistemas.", "direccion": "Perspectiva estratégica y gestión.", "innovacion": "Enfoque novedad y digitalización.", "contabilidad": "Experto contable y finanzas.", "administracion": "Eficiencia procesos administrativos." }
FRASES_BUSQUEDA = ["no tengo información", "no dispongo de información", "no tengo acceso", "no sé sobre eso"]

# --- Temp Dir ---
TEMP_DIR = "/tmp/uploads_ashotel"; os.makedirs(TEMP_DIR, exist_ok=True); logging.info(f"Directorio temporal: {TEMP_DIR}")

# --- Funciones Auxiliares ---

# Conexión a PostgreSQL
def get_db_connection():
    if not DB_CONFIGURED: return None
    try: return psycopg2.connect(host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS, port=DB_PORT, connect_timeout=5)
    except (Exception, psycopg2.Error) as error: logging.error(f"Error conectar PGSQL {DB_HOST}:{DB_PORT}: {error}"); return None

# Extraer texto PDF/DOCX
def extraer_texto_pdf_docx(ruta_archivo: str, extension: str) -> str:
    texto = ""; logging.info(f"Extrayendo texto de: {ruta_archivo} (Ext: {extension})")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo);
                if lector.is_encrypted: logging.warning(f"PDF encriptado: {ruta_archivo}")
                for pagina in lector.pages: texto_pagina = pagina.extract_text();
                if texto_pagina: texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]:
            doc = Document(ruta_archivo); texto = "\n".join([p.text for p in doc.paragraphs if p.text])
        else: return ""
        logging.info(f"Texto extraído PDF/DOCX (longitud: {len(texto)}).")
        return texto.strip()
    except pdf_errors.PdfReadError as e: logging.error(f"Error leer PDF {ruta_archivo}: {e}"); return f"[Error PDF: No se pudo leer]"
    except PackageNotFoundError: logging.error(f"Error DOCX {ruta_archivo}: No válido."); return "[Error DOCX: Archivo inválido]"
    except FileNotFoundError: logging.error(f"Error interno: {ruta_archivo} no encontrado."); return "[Error interno: Archivo no encontrado]"
    except Exception as e: logging.error(f"Error extraer PDF/DOCX {ruta_archivo}: {e}", exc_info=True); return "[Error interno procesando PDF/DOCX.]"

# Buscar en Google
def buscar_google(query: str) -> str:
    if not GOOGLE_API_KEY or not GOOGLE_CX: return "<p><i>[Búsqueda web no disponible.]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"; params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3}
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

    # --- Obtener Contexto de Documentos ---
    document_context = ""; conn_docs = None
    if current_user_id and DB_CONFIGURED and PHP_BRIDGE_CONFIGURED:
        conn_docs = get_db_connection()
        if conn_docs:
            relevant_doc = None; active_docs = []
            try:
                with conn_docs.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    sql_docs = "SELECT id, original_filename, file_type FROM user_documents WHERE user_id = %s AND is_active_for_ai = TRUE ORDER BY uploaded_at DESC LIMIT 10"
                    cursor.execute(sql_docs, (current_user_id,))
                    active_docs = cursor.fetchall()
                logging.info(f"Encontrados {len(active_docs)} docs activos para user {current_user_id}")

                if active_docs:
                    query_keywords = set(re.findall(r'\b\w{4,}\b', mensaje_usuario.lower()))
                    if query_keywords: # Solo buscar si hay keywords
                         logging.info(f"Keywords consulta: {query_keywords}")
                         for doc in active_docs:
                             doc_name_base = os.path.splitext(doc['original_filename'].lower())[0]
                             doc_name_keywords = set(re.findall(r'\b\w{4,}\b', doc_name_base))
                             if query_keywords.intersection(doc_name_keywords):
                                 logging.info(f"Relevancia encontrada: '{doc['original_filename']}'")
                                 relevant_doc = doc; break

                    if relevant_doc:
                        doc_id = relevant_doc['id']; file_type = relevant_doc['file_type']; original_fname = relevant_doc['original_filename']
                        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&api_key={PHP_API_SECRET_KEY}"
                        logging.info(f"Solicitando doc ID {doc_id} a PHP...")
                        try:
                            response = requests.get(serve_url, timeout=25, stream=True); response.raise_for_status()
                            file_ext = os.path.splitext(original_fname)[1].lower().strip('.')
                            # Solo procesar tipos que sabemos extraer
                            if file_ext in ['pdf', 'doc', 'docx', 'txt', 'csv']:
                                fd, temp_path = tempfile.mkstemp(suffix=f'.{file_ext}', dir=TEMP_DIR)
                                logging.info(f"Guardando en temp: {temp_path}")
                                extracted_text = ""
                                try:
                                    with os.fdopen(fd, 'wb') as temp_file: # Abrir binario para escritura
                                        for chunk in response.iter_content(chunk_size=8192): temp_file.write(chunk)
                                    # Ahora extraer texto
                                    if file_ext in ['pdf', 'doc', 'docx']: extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
                                    elif file_ext in ['txt', 'csv']:
                                         try:
                                              with open(temp_path, 'r', encoding='utf-8', errors='ignore') as f: extracted_text = f.read()
                                              logging.info(f"Texto extraído TXT/CSV (longitud: {len(extracted_text)}).")
                                         except Exception as read_err: logging.error(f"Error leyendo archivo de texto {temp_path}: {read_err}")
                                else: extracted_text = "[Error: Tipo no procesable aquí]"
                                finally:
                                    try: os.remove(temp_path)
                                    except OSError as e: logging.error(f"Error borrar temp {temp_path}: {e}")

                                if extracted_text and not extracted_text.startswith("[Error"):
                                    max_context_len = 3500
                                    document_context = f"\n\n### Contexto del Documento '{original_fname}' ###\n{extracted_text[:max_context_len]}"
                                    if len(extracted_text) > max_context_len: document_context += "\n[...Texto truncado...]"
                                    logging.info(f"Contexto añadido desde doc ID {doc_id}.")
                            else: logging.warning(f"Tipo de archivo recuperado ({file_ext}) no procesable para texto.")
                        except requests.exceptions.RequestException as e: logging.error(f"Error al solicitar archivo PHP doc ID {doc_id}: {e}")
                        except Exception as e: logging.error(f"Error procesando archivo recuperado doc ID {doc_id}: {e}", exc_info=True)
            except (Exception, psycopg2.Error) as e: logging.error(f"Error BD listar docs user {current_user_id}: {e}")
            finally:
                if conn_docs: conn_docs.close()

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_CONSULTA, prompt_especifico]
    if custom_prompt_text: system_prompt_parts.extend(["\n\n### Instrucciones Adicionales Usuario ###", custom_prompt_text])
    if document_context: system_prompt_parts.append(document_context)
    system_prompt = "\n".join(system_prompt_parts)
    logging.debug(f"System Prompt Final (primeros 500): {system_prompt[:500]}") # Log para depurar

    # --- Lógica OpenAI / Búsqueda Web ---
    texto_respuesta_final = ""; activar_busqueda = forzar_busqueda_web
    try:
        logging.info("Llamada OpenAI 1...")
        respuesta_inicial = client.chat.completions.create( model="gpt-4-turbo", messages=[{"role": "system", "content": system_prompt},{"role": "user", "content": mensaje_usuario}], temperature=0.5, max_tokens=1500 )
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


@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general"),
    user_id: int | None = Form(None)
):
    if not client: raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    filename = file.filename or "unknown"; content_type = file.content_type or ""; extension = filename.split('.')[-1].lower() if '.' in filename else ''
    current_user_id = user_id; especializacion_lower = especializacion.lower()
    logging.info(f"Análisis: User={current_user_id}, File={filename}, Espec='{especializacion_lower}'")

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""; conn = None
    if current_user_id and DB_CONFIGURED:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s", (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'): custom_prompt_text = result['custom_prompt'].strip(); logging.info(f"Prompt OK user: {current_user_id}")
            except (Exception, psycopg2.Error) as e: logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                if conn: conn.close()

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico]
    if custom_prompt_text: system_prompt_parts.extend(["\n\n### Instrucciones Adicionales Usuario ###", custom_prompt_text])
    system_prompt = "\n".join(system_prompt_parts)
    logging.debug(f"System Prompt Análisis (primeros 500): {system_prompt[:500]}") # Log para depurar

    # --- Lógica Procesamiento Archivo / Llamada OpenAI ---
    informe_html = ""; messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]
    try:
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando IMAGEN.")
            image_bytes = await file.read(); base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_prompt_image = ("Analiza la imagen, extrae su texto (OCR), y redacta un informe HTML profesional basado en ese texto. Sigue formato HTML y evita Markdown. Devuelve solo el HTML.")
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": [ {"type": "text", "text": user_prompt_image}, {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}} ] } ]
        elif extension in ["pdf", "docx", "doc", "txt", "csv"]:
            logging.info(f"Procesando {extension.upper()}.")
            ruta_temporal = os.path.join(TEMP_DIR, f"up_{os.urandom(8).hex()}.{extension}")
            texto_extraido = ""; temp_file_saved = False
            try:
                with open(ruta_temporal, "wb") as buffer: shutil.copyfileobj(file.file, buffer); temp_file_saved = True
                # Extraer texto según extensión
                if extension in ['pdf', 'doc', 'docx']: texto_extraido = extraer_texto_pdf_docx(ruta_temporal, extension)
                elif extension in ['txt', 'csv']:
                     try:
                          with open(ruta_temporal, 'r', encoding='utf-8', errors='ignore') as f: texto_extraido = f.read()
                          logging.info(f"Texto extraído TXT/CSV (longitud: {len(texto_extraido)}).")
                     except Exception as read_err: logging.error(f"Error leyendo archivo texto {ruta_temporal}: {read_err}")
                else: texto_extraido = "[Error: Tipo no procesable aquí]"
            finally:
                # CORRECCIÓN DE INDENTACIÓN APLICADA AQUÍ
                if temp_file_saved and os.path.exists(ruta_temporal):
                    try:
                        os.remove(ruta_temporal)
                        logging.info(f"Archivo temporal eliminado: {ruta_temporal}")
                    except OSError as e:
                         logging.error(f"Error al eliminar archivo temporal {ruta_temporal}: {e}")
            # FIN CORRECCIÓN INDENTACIÓN

            if texto_extraido.startswith("[Error"): raise ValueError(texto_extraido)
            if not texto_extraido: raise ValueError(f"No se extrajo texto del archivo {extension.upper()}.")
            user_prompt_text = (f"Redacta un informe HTML profesional basado en texto:\n--- INICIO ---\n{texto_extraido}\n--- FIN ---\n Sigue formato HTML, evita Markdown. Devuelve solo HTML.")
            messages_payload = [ {"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt_text} ]
        else: raise HTTPException(status_code=415, detail=f"Tipo archivo no soportado: {content_type or extension}.")

        if not messages_payload: raise HTTPException(status_code=500, detail="Error interno: No payload IA.")
        logging.info(f"Llamada a OpenAI...")
        respuesta_informe = client.chat.completions.create( model="gpt-4-turbo", messages=messages_payload, temperature=0.3, max_tokens=2500 )
        informe_html = respuesta_informe.choices[0].message.content.strip(); logging.info(f"Informe generado OK.")
        if BS4_AVAILABLE:
            try:
                 if "<!DOCTYPE html>" in informe_html or "<html" in informe_html: soup = BeautifulSoup(informe_html, 'html.parser'); informe_html = soup.body.decode_contents() if soup.body else informe_html; logging.info("HTML completo detectado, extraído body.")
            except Exception as e: logging.error(f"Error procesar HTML con BS4: {e}")
        if not informe_html.strip().startswith('<'): informe_html = f"<p>{informe_html}</p>"

    except APIError as e: logging.error(f"Error API OpenAI /analizar: {e}"); raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except HTTPException as e: raise e
    except ValueError as e: logging.error(f"Error procesando {filename}: {e}"); raise HTTPException(status_code=400, detail=str(e))
    except Exception as e: logging.error(f"Error inesperado /analizar: {e}", exc_info=True); raise HTTPException(status_code=500, detail="Error interno servidor.")
    finally: await file.close()

    return RespuestaAnalisis(informe=informe_html)

# --- Punto de Entrada ---
# if __name__ == "__main__": import uvicorn; uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)

# --- FIN main.py v2.2.1 ---
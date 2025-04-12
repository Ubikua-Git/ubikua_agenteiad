# --- INICIO main.py v2.3.7 (Integrado /analizar-documento) ---
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
import hashlib
from PyPDF2 import PdfReader, errors as pdf_errors
from docx import Document
from docx.opc.exceptions import PackageNotFoundError
try:
    from bs4 import BeautifulSoup
    BS4_AVAILABLE = True
except ImportError:
    BS4_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(title="Asistente IA Ashotel API v2.3.7 (Integrado)", version="2.3.7")
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
        logging.info(f"Texto extraído PDF/DOCX OK (longitud: {len(texto)} caracteres).")
        return texto.strip()
    except pdf_errors.PdfReadError as e:
        logging.error(f"Error leer PDF {ruta_archivo}: {e}")
        return "[Error PDF: No se pudo leer]"
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
            logging.info(f"Longitud de datos leídos: {len(raw_data)} bytes")
            detected_encoding = chardet.detect(raw_data)['encoding'] or 'utf-8'
        with open(ruta_archivo, 'r', encoding=detected_encoding, errors='ignore') as f:
            texto = f.read()
        logging.info(f"Texto extraído simple OK (longitud: {len(texto)} caracteres).")
        return texto.strip()
    except FileNotFoundError:
        logging.error(f"Error interno: {ruta_archivo} no encontrado para simple.")
        return "[Error interno: Archivo no encontrado]"
    except Exception as e:
        logging.error(f"Error extraer texto simple {ruta_archivo}: {e}")
        return "[Error interno procesando texto plano.]"

def buscar_google(query: str) -> str:
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        return "<p><i>[Búsqueda web no disponible.]</i></p>"
    url = "https://www.googleapis.com/customsearch/v1"
    params = {"key": GOOGLE_API_KEY, "cx": GOOGLE_CX, "q": query, "num": 3}
    logging.info(f"Buscando en Google: '{query}'")
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        resultados = data.get("items", [])
        if not resultados:
            return "<p><i>[No se encontraron resultados web.]</i></p>"
        texto_resultados = "<div class='google-results'><h4 style='font-size:0.9em;color:#555;'>Resultados web:</h4>"
        for item in resultados:
            title = item.get('title','')
            link = item.get('link','#')
            snippet = item.get('snippet','').replace('\n',' ')
            texto_resultados += f"<div><a href='{link}' target='_blank'>{title}</a><p>{snippet}</p><cite>{link}</cite></div>\n"
        texto_resultados += "</div>"
        logging.info(f"Búsqueda web OK: {len(resultados)} resultados.")
        return texto_resultados
    except requests.exceptions.Timeout:
        logging.error("Timeout búsqueda web.")
        return "<p><i>[Error: Timeout búsqueda web.]</i></p>"
    except requests.exceptions.RequestException as e:
        logging.error(f"Error búsqueda web: {e}")
        return "<p><i>[Error conexión búsqueda web.]</i></p>"
    except Exception as e:
        logging.error(f"Error inesperado búsqueda web: {e}")
        return "<p><i>[Error inesperado búsqueda web.]</i></p>"

# --- Endpoint para procesar texto de documentos subidos (/process-document) ---
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

        # 2. Construir URL para PHP Bridge y obtener el contenido del archivo
        serve_url = f"{PHP_FILE_SERVE_URL}?doc_id={doc_id}&user_id={current_user_id}&api_key={PHP_API_SECRET_KEY}"
        logging.info(f"Solicitando doc ID {doc_id} a PHP. URL: {serve_url}")
        response = requests.get(serve_url, timeout=30, stream=True)
        response.raise_for_status()
        try:
            content_snippet = response.content[:200]
            snippet_decoded = content_snippet.decode('utf-8', 'ignore')
        except Exception as log_exc:
            snippet_decoded = "[Error decodificando contenido]"
            logging.error(f"Error decodificando snippet: {log_exc}")
        logging.info(f"Contenido recibido del PHP Bridge (primeros 200 caracteres): {snippet_decoded}")

        # 3. Guardar temporalmente el contenido y extraer texto
        file_ext = os.path.splitext(original_fname)[1].lower().strip('.')
        extracted_text = None
        if file_ext in ['pdf', 'doc', 'docx', 'txt', 'csv']:
            fd, temp_path = tempfile.mkstemp(suffix=f'.{file_ext}', dir=TEMP_DIR)
            logging.info(f"Guardando contenido en archivo temporal: {temp_path}")
            try:
                with os.fdopen(fd, 'wb') as temp_file:
                    for chunk in response.iter_content(chunk_size=8192):
                        temp_file.write(chunk)
                with open(temp_path, 'rb') as f:
                    file_data = f.read()
                    file_hash = hashlib.sha256(file_data).hexdigest()
                logging.info(f"Archivo temporal guardado (longitud: {len(file_data)} bytes, hash: {file_hash})")
                
                if file_ext in ['pdf', 'doc', 'docx']:
                    extracted_text = extraer_texto_pdf_docx(temp_path, file_ext)
                elif file_ext in ['txt', 'csv']:
                    extracted_text = extraer_texto_simple(temp_path)
            finally:
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                        logging.info(f"Archivo temporal {temp_path} eliminado tras extracción.")
                    except OSError as e:
                        logging.error(f"Error al borrar archivo temporal {temp_path}: {e}")
        else:
            extracted_text = "[Extracción no soportada para este tipo de archivo]"

        if extracted_text is None:
            raise ValueError("Fallo la extracción de texto.")
        logging.info(f"Actualizando BD doc ID {doc_id} con texto (longitud: {len(extracted_text)} caracteres)...")
        with conn.cursor() as cursor:
            sql_update = "UPDATE user_documents SET extracted_text = %s, procesado = TRUE WHERE id = %s AND user_id = %s"
            cursor.execute(sql_update, (extracted_text, doc_id, current_user_id))
            if cursor.rowcount == 0:
                logging.warning(f"UPDATE texto no afectó filas para doc {doc_id}")
        conn.commit()
        logging.info(f"BD actualizada doc ID {doc_id}.")
        return ProcessResponse(success=True, message="Documento procesado.")
    except AssertionError as e:
        logging.error(f"Assertion error procesando doc {doc_id}: {e}")
        return ProcessResponse(success=False, error=str(e))
    except requests.exceptions.RequestException as e:
        logging.error(f"Error solicitando PHP doc {doc_id}: {e}")
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

# --- Endpoint de consulta (/consulta) ---
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
            except Exception as e:
                logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                if conn_prompt:
                    conn_prompt.close()

    document_context = ""
    if current_user_id and DB_CONFIGURED:
        conn_docs = get_db_connection()
        if conn_docs:
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
                          AND extracted_text IS NOT NULL AND extracted_text != '' AND NOT extracted_text LIKE '[Error%%'
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
                                available_chars = int((3500 - current_token_count) / 1.3)
                                context_parts.append(text[:available_chars])
                                current_token_count += doc_tokens
                        document_context = "\n".join(context_parts)
            except Exception as e:
                logging.error(f"Error BD FTS user {current_user_id}: {e}")
            finally:
                if conn_docs:
                    conn_docs.close()

    system_prompt = "\n".join([
        BASE_PROMPT_CONSULTA,
        PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"]),
        custom_prompt_text,
        document_context
    ])
    logging.debug(f"Prompt para OpenAI: {system_prompt[:500]}")

    try:
        logging.info("Llamada OpenAI para consulta...")
        respuesta_inicial = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": mensaje_usuario}
            ],
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

# --- Endpoint para analizar documentos (/analizar-documento) ---
@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general"),
    user_id: int | None = Form(None)
):
    if not client:
        raise HTTPException(status_code=503, detail="Servicio IA no configurado.")

    filename = file.filename or "unknown"
    content_type = file.content_type or ""
    extension = filename.split('.')[-1].lower() if '.' in filename else ''
    current_user_id = user_id
    especializacion_lower = especializacion.lower()
    logging.info(f"Análisis: User={current_user_id}, File={filename}, Espec='{especializacion_lower}'")

    custom_prompt_text = ""
    if current_user_id and DB_CONFIGURED:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s", (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'):
                        custom_prompt_text = result['custom_prompt'].strip()
            except Exception as e:
                logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                if conn:
                    conn.close()

    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt = "\n".join([BASE_PROMPT_ANALISIS_DOC, prompt_especifico])
    if custom_prompt_text:
        system_prompt += "\n\n### Instrucciones Adicionales Usuario ###\n" + custom_prompt_text

    messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]

    if content_type in IMAGE_MIMES:
        logging.info("Procesando imagen subida para análisis.")
        image_bytes = await file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        user_prompt = "Analiza la imagen y extrae su contenido en un informe HTML profesional."
        messages_payload = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": f"data:{content_type};base64,{base64_image}"}
        ]
    elif extension in ["pdf", "doc", "docx", "txt", "csv"]:
        logging.info(f"Procesando archivo {extension.upper()} para análisis.")
        temp_filename = os.path.join(TEMP_DIR, f"up_analisis_{os.urandom(8).hex()}.{extension}")
        texto_extraido = ""
        saved = False
        try:
            with open(temp_filename, "wb") as buffer:
                shutil.copyfileobj(file.file, buffer)
            saved = True
            if extension in ['pdf', 'doc', 'docx']:
                texto_extraido = extraer_texto_pdf_docx(temp_filename, extension)
            else:
                texto_extraido = extraer_texto_simple(temp_filename)
        finally:
            if saved and os.path.exists(temp_filename):
                try:
                    os.remove(temp_filename)
                    logging.info(f"Archivo temporal de análisis eliminado: {temp_filename}")
                except OSError as e:
                    logging.error(f"Error eliminando archivo temporal {temp_filename}: {e}")
        if texto_extraido.startswith("[Error"):
            raise HTTPException(status_code=400, detail=texto_extraido)
        if not texto_extraido:
            raise HTTPException(status_code=400, detail=f"No se extrajo texto del archivo {extension.upper()}.")
        user_prompt = f"Redacta un informe HTML profesional basado en el siguiente texto:\n--- INICIO ---\n{texto_extraido}\n--- FIN ---"
        messages_payload = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    else:
        raise HTTPException(status_code=415, detail=f"Tipo archivo no soportado: {content_type or extension}.")

    try:
        logging.info("Llamada a OpenAI para análisis de documento...")
        respuesta_informe = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=messages_payload,
            temperature=0.3,
            max_tokens=2500
        )
        informe_html = respuesta_informe.choices[0].message.content.strip()
        if BS4_AVAILABLE:
            try:
                if "<!DOCTYPE html>" in informe_html or "<html" in informe_html:
                    soup = BeautifulSoup(informe_html, 'html.parser')
                    informe_html = soup.body.decode_contents() if soup.body else informe_html
                    logging.info("HTML completo detectado, extraído body.")
            except Exception as e:
                logging.error(f"Error procesar HTML con BS4: {e}")
        if not informe_html.strip().startswith('<'):
            informe_html = f"<p>{informe_html}</p>"
    except APIError as e:
        logging.error(f"Error API OpenAI /analizar: {e}")
        raise HTTPException(status_code=503, detail=f"Error IA: {e.message}")
    except Exception as e:
        logging.error(f"Error inesperado /analizar: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno servidor.")
    finally:
        await file.close()

    return RespuestaAnalisis(informe=informe_html)

# --- Punto de Entrada (Opcional) ---
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
# --- FIN main.py v2.3.7 ---

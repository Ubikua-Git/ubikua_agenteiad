from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field # Field para validaciones si fueran necesarias
from openai import OpenAI, APIError # Importar APIError para capturar errores de OpenAI
import os
import shutil
import requests
from PyPDF2 import PdfReader, errors as pdf_errors # Importar errores específicos
from docx import Document
from docx.opc.exceptions import PackageNotFoundError # Error si no es docx válido
import pytesseract
from PIL import Image, UnidentifiedImageError # Error si no es imagen válida
import logging # Para registrar errores

# Configurar logging básico
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

app = FastAPI(
    title="Asistente IA Ashotel API",
    description="API para consultas y análisis de documentos con IA para Ashotel",
    version="1.1.0" # Incremento versión por mejoras
)

# Configuración CORS (permite cualquier origen por ahora)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # En producción, limitar a la URL del frontend
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuración de Clientes y API Keys ---
try:
    # Cargar clave API de OpenAI (más seguro desde variable de entorno)
    openai_api_key = os.getenv("OPENAI_API_KEY")
    if not openai_api_key:
        raise ValueError("Variable de entorno OPENAI_API_KEY no encontrada.")
    client = OpenAI(api_key=openai_api_key)
    logging.info("Cliente OpenAI inicializado.")

    # Claves para Google Custom Search (opcional si no se usa/configura)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
    GOOGLE_CX = os.getenv("GOOGLE_CX")
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Variables de entorno GOOGLE_API_KEY o GOOGLE_CX no encontradas. La búsqueda web no funcionará.")
        GOOGLE_API_KEY = None # Marcar como no disponible
        GOOGLE_CX = None

except ValueError as e:
    logging.error(f"Error de configuración inicial: {e}")
    # Podrías detener la app aquí si OpenAI es esencial
    # raise RuntimeError(f"Configuración crítica faltante: {e}") from e
    client = None # Marcar como no disponible si falla

except Exception as e:
    logging.error(f"Error inesperado al inicializar clientes: {e}")
    client = None
    GOOGLE_API_KEY = None
    GOOGLE_CX = None


# --- Modelos de Datos (Pydantic) ---
class PeticionConsulta(BaseModel):
    mensaje: str = Field(..., min_length=1, description="Texto de la consulta del usuario.")
    especializacion: str = Field(default="general", description="Área de especialización seleccionada.")
    buscar_web: bool = Field(default=False, description="Indica si el usuario forzó la búsqueda web.")

class RespuestaConsulta(BaseModel):
    respuesta: str

class RespuestaAnalisis(BaseModel):
    informe: str

# --- Prompts y Configuraciones IA ---
# Mensajes de sistema base
BASE_PROMPT_CONSULTA = (
    "Eres el Asistente IA oficial de Ashotel, la Asociación Hotelera y Extrahotelera de Tenerife, La Palma, La Gomera y El Hierro. "
    "Tu misión es ayudar a los distintos equipos internos de Ashotel con respuestas claras, precisas, y alineadas a sus objetivos estratégicos. "
    "Si no tienes información directa sobre temas muy específicos o actuales, indícalo claramente y, si se te proporciona contexto web, intégralo. "
    "Cuando respondas con listas estructuradas o datos comparativos, utiliza siempre tablas en formato HTML (usa <table>, <thead>, <tbody>, <tr>, <th>, <td>). "
    "Para listas simples, usa <ul> y <li>. Para enfatizar, usa <strong> o <em>. "
    "Evita usar Markdown (como asteriscos para negrita o guiones para listas). Tu respuesta debe ser directamente HTML renderizable."
)
BASE_PROMPT_ANALISIS = (
    "Eres el Asistente IA oficial de Ashotel, experto en redactar informes profesionales concisos y claros "
    "a partir de texto extraído de documentos (PDF, DOCX, imágenes). "
    "Tu tarea es analizar el texto proporcionado y generar un informe estructurado en formato HTML limpio. "
    "Usa encabezados (<h2>, <h3>), párrafos (<p>), listas (<ul>, <li>), y énfasis (<strong>, <em>) apropiadamente. "
    "La respuesta debe ser únicamente el código HTML del informe, sin explicaciones previas o posteriores como 'Aquí tienes el informe:'. "
    "Adapta ligeramente el tono y enfoque según la especialización indicada."
)

# Prompts adicionales por especialización
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

# Frases para detectar si GPT necesita buscar en la web
FRASES_BUSQUEDA = [
    "no tengo información", "no dispongo de información", "no estoy seguro",
    "no encontré datos", "no tengo acceso a información en tiempo real",
    "mi conocimiento es limitado hasta", "como modelo de lenguaje, no puedo saber"
]

# Carpeta temporal segura dentro del proyecto (si Render lo permite) o usar /tmp
TEMP_DIR = "/tmp/uploads_ashotel"
os.makedirs(TEMP_DIR, exist_ok=True)
logging.info(f"Directorio temporal creado/asegurado: {TEMP_DIR}")

# --- Funciones Auxiliares ---

def extraer_texto(ruta_archivo: str, extension: str) -> str:
    """Extrae texto de PDF, DOCX o Imagenes (usando OCR)."""
    texto = ""
    logging.info(f"Extrayendo texto de: {ruta_archivo} (Ext: {extension})")
    try:
        if extension == "pdf":
            with open(ruta_archivo, 'rb') as archivo:
                lector = PdfReader(archivo)
                if lector.is_encrypted:
                    logging.warning(f"El PDF {ruta_archivo} está encriptado. Intentando leer sin contraseña.")
                    # Podrías intentar desbloquearlo si tuvieras la contraseña:
                    # lector.decrypt('')
                for pagina in lector.pages:
                    texto_pagina = pagina.extract_text()
                    if texto_pagina:
                        texto += texto_pagina + "\n"
        elif extension in ["doc", "docx"]:
            doc = Document(ruta_archivo)
            texto = "\n".join([parrafo.text for parrafo in doc.paragraphs if parrafo.text])
        elif extension in ["png", "jpg", "jpeg", "webp", "tiff", "bmp"]: # Añadir más formatos si es necesario
            # Verificar si Tesseract está disponible
            try:
                pytesseract.get_tesseract_version()
            except pytesseract.TesseractNotFoundError:
                 logging.error("Tesseract no está instalado o no se encuentra en el PATH.")
                 return "[Error: Tesseract OCR no está disponible en el servidor]"

            imagen = Image.open(ruta_archivo)
            # Intentar OCR en español, fallback a inglés si falla
            try:
                 texto = pytesseract.image_to_string(imagen, lang="spa")
                 logging.info("OCR realizado con éxito (idioma: spa).")
            except pytesseract.TesseractError as ocr_error:
                 logging.warning(f"Error OCR con 'spa' para {ruta_archivo}: {ocr_error}. Intentando con 'eng'.")
                 try:
                     texto = pytesseract.image_to_string(imagen, lang="eng")
                     logging.info("OCR realizado con éxito (idioma: eng).")
                 except pytesseract.TesseractError as ocr_error_eng:
                     logging.error(f"Error OCR también con 'eng' para {ruta_archivo}: {ocr_error_eng}")
                     texto = "[Error: No se pudo extraer texto de la imagen con OCR]"
            except Exception as img_ocr_err: # Otro error inesperado con Tesseract/PIL
                 logging.error(f"Error inesperado durante OCR de {ruta_archivo}: {img_ocr_err}")
                 texto = "[Error: Problema inesperado durante el OCR de la imagen]"

        else:
             logging.warning(f"Extensión de archivo no soportada para extracción de texto: {extension}")
             return f"[Error: Tipo de archivo '{extension}' no soportado para análisis]"

        logging.info(f"Texto extraído con éxito (longitud: {len(texto)} caracteres).")
        return texto.strip()

    except pdf_errors.PdfReadError as e:
        logging.error(f"Error al leer PDF {ruta_archivo}: {e}")
        return "[Error: No se pudo leer el archivo PDF, puede estar dañado o protegido.]"
    except PackageNotFoundError:
        logging.error(f"Error: El archivo {ruta_archivo} no parece ser un DOCX válido.")
        return "[Error: El archivo no es un formato DOCX válido.]"
    except UnidentifiedImageError:
         logging.error(f"Error: No se pudo identificar o abrir el archivo de imagen {ruta_archivo}.")
         return "[Error: No se pudo abrir o identificar el archivo como imagen válida.]"
    except FileNotFoundError:
         logging.error(f"Error: Archivo temporal no encontrado en {ruta_archivo}.")
         return "[Error interno: Archivo temporal no encontrado]"
    except Exception as e:
        logging.error(f"Error inesperado al extraer texto de {ruta_archivo}: {e}")
        # En producción, no devolver detalles del error al usuario directamente por seguridad
        return "[Error interno procesando el archivo. Contacte al administrador.]"


def buscar_google(query: str) -> str:
    """Realiza una búsqueda en Google y devuelve los 3 primeros resultados como HTML."""
    if not GOOGLE_API_KEY or not GOOGLE_CX:
        logging.warning("Intento de búsqueda web sin claves API de Google configuradas.")
        return "<p><i>[Búsqueda web no disponible en la configuración actual del servidor.]</i></p>"

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

        # Formatear resultados como HTML
        texto_resultados = "<div class='google-results' style='margin-top: 15px; border-top: 1px solid #eee; padding-top: 10px;'>"
        texto_resultados += "<h4 style='font-size: 0.9em; color: #555; margin-bottom: 8px;'>Resultados de búsqueda web:</h4>"
        for item in resultados:
            title = item.get('title', 'Sin título')
            link = item.get('link', '#')
            snippet = item.get('snippet', 'Sin descripción').replace('\n', ' ') # Limpiar saltos de línea

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

    especializacion = datos.especializacion.lower()
    mensaje_usuario = datos.mensaje
    forzar_busqueda_web = datos.buscar_web

    logging.info(f"Recibida consulta: Especialización='{especializacion}', BuscarWebForzado={forzar_busqueda_web}")
    logging.debug(f"Mensaje: {mensaje_usuario}")

    # Construir el prompt del sistema
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt = f"{BASE_PROMPT_CONSULTA}\n{prompt_especifico}"

    texto_respuesta_final = ""
    activar_busqueda = forzar_busqueda_web
    web_resultados_html = ""

    try:
        # Primera llamada a GPT-4 para obtener una respuesta inicial
        logging.info("Realizando primera llamada a OpenAI...")
        respuesta_inicial = client.chat.completions.create(
            model="gpt-4-turbo", # O el modelo que prefieras/tengas disponible
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": mensaje_usuario}
            ],
            temperature=0.5, # Ajustar creatividad/precisión
            max_tokens=1500 # Limitar longitud de respuesta
        )
        texto_respuesta_inicial = respuesta_inicial.choices[0].message.content.strip()
        logging.info("Primera respuesta de OpenAI recibida.")
        logging.debug(f"Respuesta inicial: {texto_respuesta_inicial[:100]}...") # Loguear inicio de respuesta

        # Comprobar si es necesario buscar en la web (si no se forzó ya)
        if not activar_busqueda:
            respuesta_lower = texto_respuesta_inicial.lower()
            if any(frase in respuesta_lower for frase in FRASES_BUSQUEDA):
                logging.info("Respuesta inicial indica falta de información. Activando búsqueda web.")
                activar_busqueda = True

        # Si se activa la búsqueda (forzada o automática)
        if activar_busqueda:
            logging.info("Iniciando búsqueda web...")
            web_resultados_html = buscar_google(mensaje_usuario)

            if "[Error" not in web_resultados_html: # Solo si la búsqueda fue exitosa
                 logging.info("Realizando segunda llamada a OpenAI con contexto web...")
                 # Segunda llamada con contexto web
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
                 # Si la búsqueda falló, añadir mensaje de error y usar respuesta inicial
                 logging.warning("Búsqueda web falló, se usará la respuesta inicial con nota de error.")
                 texto_respuesta_final = texto_respuesta_inicial + "\n" + web_resultados_html
        else:
             # Si no se necesitó búsqueda, la respuesta final es la inicial
             texto_respuesta_final = texto_respuesta_inicial

    except APIError as e:
        logging.error(f"Error de API OpenAI en /consulta: {e}")
        raise HTTPException(status_code=503, detail=f"Error al contactar el servicio IA: {e.message}")
    except Exception as e:
        logging.error(f"Error inesperado en /consulta: {e}", exc_info=True) # Log completo con traceback
        raise HTTPException(status_code=500, detail="Error interno del servidor al procesar la consulta.")

    # Devolver la respuesta final en el formato esperado
    return RespuestaConsulta(respuesta=texto_respuesta_final)


@app.post("/analizar-documento", response_model=RespuestaAnalisis)
async def analizar_documento(
    file: UploadFile = File(..., description="Archivo a analizar (PDF, DOCX, PNG, JPG)."),
    especializacion: str = Form(default="general", description="Área de especialización para enfocar el informe.")
):
    """Recibe un archivo, extrae texto y genera un informe HTML con IA."""
    if not client:
         logging.error("Intento de análisis sin cliente OpenAI inicializado.")
         raise HTTPException(status_code=503, detail="Servicio IA no disponible (Error de configuración).")

    extension = file.filename.split('.')[-1].lower() if '.' in file.filename else ''
    ruta_temporal = os.path.join(TEMP_DIR, f"upload_{os.urandom(8).hex()}.{extension}")
    logging.info(f"Recibido archivo: {file.filename}, Tipo: {file.content_type}, Tamaño: {file.size}, Guardando en: {ruta_temporal}")

    # Guardar archivo temporalmente
    try:
        with open(ruta_temporal, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
        logging.info("Archivo guardado temporalmente.")
    except Exception as e:
         logging.error(f"Error al guardar archivo temporal {ruta_temporal}: {e}")
         raise HTTPException(status_code=500, detail="Error interno al guardar el archivo subido.")
    finally:
        # Asegurarse de cerrar el archivo subido
        await file.close()

    # Extraer texto
    texto_extraido = extraer_texto(ruta_temporal, extension)

    # Limpiar archivo temporal SIEMPRE, incluso si la extracción falló
    try:
        os.remove(ruta_temporal)
        logging.info(f"Archivo temporal eliminado: {ruta_temporal}")
    except OSError as e:
         logging.error(f"Error al eliminar archivo temporal {ruta_temporal}: {e}")
         # No lanzar excepción aquí, el proceso principal puede continuar si hubo extracción

    # Si hubo error en la extracción, devolverlo directamente
    if texto_extraido.startswith("[Error"):
        logging.warning(f"Extracción fallida para {file.filename}. Razón: {texto_extraido}")
        # Devolver el error como "informe" para que el usuario lo vea
        return RespuestaAnalisis(informe=f"<p class='text-red-500'>{texto_extraido}</p>")
    elif not texto_extraido:
         logging.warning(f"No se extrajo texto del archivo {file.filename}.")
         return RespuestaAnalisis(informe="<p class='text-orange-500'>[Advertencia: No se pudo extraer contenido textual del archivo.]</p>")

    logging.info(f"Texto extraído para {file.filename} (longitud: {len(texto_extraido)}). Generando informe...")
    logging.debug(f"Texto extraído (primeros 200 chars): {texto_extraido[:200]}...")

    # Construir prompts para OpenAI
    especializacion_lower = especializacion.lower()
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt = f"{BASE_PROMPT_ANALISIS}\n{prompt_especifico}"
    user_prompt = (
        "Por favor, redacta un informe profesional claro y bien estructurado en formato HTML basado únicamente en el siguiente texto extraído del documento. "
        "Sigue las instrucciones de formato HTML (<h1>, <h2>, <p>, <ul>, <li>, <strong>, <em>) y evita Markdown.\n\n"
        "--- INICIO TEXTO EXTRAÍDO ---\n"
        f"{texto_extraido}\n"
        "--- FIN TEXTO EXTRAÍDO ---\n\n"
        "Recuerda: Devuelve solo el HTML del informe."
    )

    try:
        # Llamada a OpenAI para generar el informe
        respuesta_informe = client.chat.completions.create(
            model="gpt-4-turbo", # O gpt-3.5-turbo si necesitas ahorrar costes
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.3, # Más factual para informes
            max_tokens=2000 # Ajustar según longitud esperada
        )
        informe_html = respuesta_informe.choices[0].message.content.strip()
        logging.info(f"Informe generado con éxito para {file.filename}.")

        # Limpieza básica del HTML (quitar posible explicación inicial/final si IA la añade)
        if informe_html.lower().startswith("aquí tienes el informe"):
            informe_html = informe_html[informe_html.find('<'):] # Quitar texto antes del primer <
        if not informe_html.startswith('<'): # Si no empieza con HTML, envolver en <p>
             informe_html = f"<p>{informe_html}</p>"


    except APIError as e:
        logging.error(f"Error de API OpenAI en /analizar-documento: {e}")
        raise HTTPException(status_code=503, detail=f"Error al contactar el servicio IA para generar el informe: {e.message}")
    except Exception as e:
        logging.error(f"Error inesperado en /analizar-documento: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno del servidor al generar el informe.")

    return RespuestaAnalisis(informe=informe_html)

# --- Punto de Entrada (Opcional, para debug local) ---
# Si quieres ejecutar localmente: uvicorn main:app --reload
# if __name__ == "__main__":
#     import uvicorn
#     uvicorn.run(app, host="0.0.0.0", port=8000)
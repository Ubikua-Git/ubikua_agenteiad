from fastapi import FastAPI, File, UploadFile, Form
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os, shutil, requests
from PyPDF2 import PdfReader
from docx import Document
import pytesseract
from PIL import Image

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
GOOGLE_CX = os.getenv("GOOGLE_CX")

class Peticion(BaseModel):
    mensaje: str
    especializacion: str = "general"
    buscar_web: bool = False

def extraer_texto(ruta_archivo, extension):
    texto = ""
    if extension == "pdf":
        with open(ruta_archivo, 'rb') as archivo:
            lector = PdfReader(archivo)
            for pagina in lector.pages:
                texto += pagina.extract_text()
    elif extension in ["doc", "docx"]:
        doc = Document(ruta_archivo)
        texto = "\n".join([parrafo.text for parrafo in doc.paragraphs])
    elif extension in ["png", "jpg", "jpeg"]:
        imagen = Image.open(ruta_archivo)
        texto = pytesseract.image_to_string(imagen, lang="spa")
    return texto

prompt_especializaciones = {
    "comunicacion": "Eres un experto en Comunicación, especializado en relaciones públicas, marketing y redacción publicitaria.",
    "formacion": "Eres un experto en Formación, especializado en pedagogía, metodologías educativas y diseño de cursos.",
    "informatica": "Eres un experto en Informática, especializado en tecnología, desarrollo de software y soporte técnico.",
    "direccion": "Eres un experto en Dirección, especializado en liderazgo, estrategia empresarial y gestión organizacional.",
    "innovacion": "Eres un experto en Innovación, especializado en tendencias tecnológicas, creatividad empresarial y transformación digital.",
    "contabilidad": "Eres un experto en Contabilidad, especializado en finanzas, análisis contable y gestión económica.",
    "administracion": "Eres un experto en Administración, especializado en procesos, organización y gestión empresarial.",
    "legal": "Eres un experto jurídico en el Departamento Legal, especializado en normativas, redacción de documentos legales y asesoramiento institucional."
}

def buscar_google(query):
    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": GOOGLE_API_KEY,
        "cx": GOOGLE_CX,
        "q": query
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        resultados = data.get("items", [])
        texto = ""
        for item in resultados[:3]:
            texto += f"<p><strong>{item['title']}</strong><br>{item['snippet']}<br><a href='{item['link']}' target='_blank'>{item['link']}</a></p>\n"
        return texto
    except Exception as e:
        return f"<p>Error al buscar en Google: {str(e)}</p>"

@app.post("/consulta")
def consultar_agente(datos: Peticion):
    especializacion = datos.especializacion.lower()
    mensaje = datos.mensaje
    buscar_web = bool(datos.buscar_web)

    base_prompt = (
        "Eres el Asistente IA oficial de Ashotel, la Asociación Hotelera y Extrahotelera de Tenerife, La Palma, La Gomera y El Hierro. "
        "Tu misión es ayudar a los distintos equipos internos de Ashotel con respuestas claras, precisas, y alineadas a sus objetivos estratégicos. "
        "Si no tienes información directa, debes consultar fuentes externas y ofrecer un resumen útil. "
    )
    system_prompt = f"{base_prompt} {prompt_especializaciones.get(especializacion, '')}"

    # Mostrar en logs si búsqueda web fue activada
    print("🔍 Búsqueda Web activada manualmente:", buscar_web)

    # Primera consulta al modelo
    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mensaje}
        ]
    )
    texto_respuesta = respuesta.choices[0].message.content.strip()

    # Detectar si GPT no sabe la respuesta o el usuario forzó la búsqueda web
    activar_busqueda = buscar_web or any(
        frase in texto_respuesta.lower()
        for frase in [
            "no tengo información",
            "no dispongo de información",
            "no estoy seguro",
            "no encontré datos",
            "no tengo acceso"
        ]
    )

    if activar_busqueda:
        print("🧠 Lanzando búsqueda web automática...")
        web_resultados = buscar_google(mensaje)
        contexto = f"Estos son algunos resultados obtenidos desde la web relacionados con la consulta:\n{web_resultados}"

        # Segunda consulta con contexto web
        respuesta_final = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{mensaje}\n\n{contexto}"}
            ]
        )
        texto_respuesta = respuesta_final.choices[0].message.content.strip()

    return {"respuesta": texto_respuesta}


@app.post("/analizar-documento")
async def analizar_documento(
    file: UploadFile = File(...),
    especializacion: str = Form("general")
):
    extension = file.filename.split('.')[-1].lower()
    ruta_temporal = f"/tmp/{file.filename}"
    with open(ruta_temporal, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    texto_extraido = extraer_texto(ruta_temporal, extension)

    base_prompt = (
        "Eres el Asistente IA oficial de Ashotel, encargado de redactar informes profesionales "
        "a partir de documentación técnica, educativa, administrativa o legal. "
        "Estructura siempre los informes con claridad, estilo formal y formato HTML limpio."
    )
    system_prompt = f"{base_prompt} {prompt_especializaciones.get(especializacion.lower(), '')}"

    prompt = (
        "Redacta un informe profesional claro y estructurado basado en el siguiente texto extraído. "
        "Usa formato HTML con etiquetas como <h1>, <h2>, <p>, <ul>, <li>, <strong>, <em>. "
        "No utilices Markdown ni asteriscos. Devuelve solo HTML bien formateado, sin explicación adicional.\n\n"
        f"{texto_extraido}"
    )

    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    )

    os.remove(ruta_temporal)
    return {"informe": respuesta.choices[0].message.content.strip()}

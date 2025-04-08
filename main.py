from fastapi import FastAPI, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware  # <-- Importa CORSMiddleware
from pydantic import BaseModel
from openai import OpenAI
import os, shutil
from PyPDF2 import PdfReader
from docx import Document
import pytesseract
from PIL import Image

app = FastAPI()

# Agrega la configuración de CORS (permite todas las conexiones)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Puedes restringirlo a dominios específicos si lo prefieres
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

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

@app.post("/consulta")
def consultar_agente(datos: Peticion):
    especializacion = datos.especializacion.lower()
    mensaje = datos.mensaje

    prompt_especializaciones = {
        "comunicacion": "Eres un experto en Comunicación, especializado en relaciones públicas, marketing y redacción publicitaria.",
        "formacion": "Eres un experto en Formación, especializado en pedagogía, metodologías educativas y diseño de cursos.",
        "informatica": "Eres un experto en Informática, especializado en tecnología, desarrollo de software y soporte técnico.",
        "direccion": "Eres un experto en Dirección, especializado en liderazgo, estrategia empresarial y gestión organizacional.",
        "innovacion": "Eres un experto en Innovación, especializado en tendencias tecnológicas, creatividad empresarial y transformación digital.",
        "contabilidad": "Eres un experto en Contabilidad, especializado en finanzas, análisis contable y gestión económica.",
        "administracion": "Eres un experto en Administración, especializado en procesos, organización y gestión empresarial.",
    }
    system_prompt = prompt_especializaciones.get(especializacion, "Eres un asistente versátil y confiable.")

    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": mensaje}
        ]
    )
    return {"respuesta": respuesta.choices[0].message.content.strip()}

@app.post("/analizar-documento")
async def analizar_documento(file: UploadFile = File(...)):
    extension = file.filename.split('.')[-1].lower()
    ruta_temporal = f"/tmp/{file.filename}"
    with open(ruta_temporal, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)
    texto_extraido = extraer_texto(ruta_temporal, extension)
    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": "Eres experto en redactar informes analíticos a partir de documentos proporcionados."},
            {"role": "user", "content": f"Redacta un informe analítico claro y estructurado basado en el siguiente texto extraído:\n\n{texto_extraido}"}
        ]
    )
    os.remove(ruta_temporal)
    return {"informe": respuesta.choices[0].message.content.strip()}

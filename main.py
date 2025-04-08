from fastapi import FastAPI, File, UploadFile
from openai import OpenAI
import os, shutil
from PyPDF2 import PdfReader
from docx import Document
import pytesseract
from PIL import Image

app = FastAPI()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def extraer_texto(ruta_archivo, extension):
    texto = ""
    if extension == "pdf":
        with open(ruta_archivo, 'rb') as archivo:
            lector = PdfReader(archivo)
            for pagina in lector.pages:
                texto += pagina.extract_text() or ""
    elif extension in ["doc", "docx"]:
        doc = Document(ruta_archivo)
        texto = "\n".join([parrafo.text for parrafo in doc.paragraphs])
    elif extension in ["png", "jpg", "jpeg"]:
        imagen = Image.open(ruta_archivo)
        texto = pytesseract.image_to_string(imagen, lang="spa")
    return texto

@app.post("/analizar-documento")
async def analizar_documento(file: UploadFile = File(...)):
    extension = file.filename.split('.')[-1].lower()
    ruta_temporal = f"/tmp/{file.filename}"
    with open(ruta_temporal, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    texto_extraido = extraer_texto(ruta_temporal, extension)
    
    # Comprueba que se extrajo algo
    if not texto_extraido.strip():
        texto_extraido = "No se pudo extraer texto del archivo."

    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": "Eres un experto en redactar informes analíticos a partir de documentos proporcionados."},
            {"role": "user", "content": f"Redacta un informe analítico claro y estructurado basado en el siguiente texto extraído:\n\n{texto_extraido}"}
        ]
    )

    os.remove(ruta_temporal)
    return {"informe": respuesta.choices[0].message.content.strip()}

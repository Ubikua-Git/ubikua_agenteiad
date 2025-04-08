from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
import os
import requests

app = FastAPI()

# Cliente OpenAI con API Key
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Variables Google Search (claramente añadidas aquí)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

GOOGLE_CX = "0650d571365cd4765"

class Peticion(BaseModel):
    mensaje: str
    buscar_web: bool = False  # Indica si debe buscar en la web

def buscar_google(termino):
    url = f"https://www.googleapis.com/customsearch/v1?key={GOOGLE_API_KEY}&cx={GOOGLE_CX}&q={termino}"
    resultado = requests.get(url).json()
    snippets = [item['snippet'] for item in resultado.get('items', [])[:3]]
    return "\n".join(snippets)

@app.post("/consulta")
def consultar_agente(datos: Peticion):
    contenido_mensaje = datos.mensaje

    # Si el usuario pidió búsqueda web explícitamente
    if datos.buscar_web:
        info_web = buscar_google(contenido_mensaje)
        contenido_mensaje += f"\n\nInformación reciente desde la web:\n{info_web}"

    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": (
                "Eres experto en marketing, redes sociales y eventos turísticos para Ashotel. "
                "Integra claramente cualquier información adicional proporcionada."
            )},
            {"role": "user", "content": contenido_mensaje}
        ]
    )
    return {"respuesta": respuesta.choices[0].message.content.strip()}

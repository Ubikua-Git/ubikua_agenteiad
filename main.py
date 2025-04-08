from fastapi import FastAPI
from pydantic import BaseModel
from openai import OpenAI
import os

app = FastAPI()

# Usa una variable de entorno para tu API Key por seguridad
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

class Peticion(BaseModel):
    mensaje: str

@app.post("/consulta")
def consultar_agente(datos: Peticion):
    respuesta = client.chat.completions.create(
        model="gpt-4-turbo",
        messages=[
            {"role": "system", "content": (
                "Eres un experto en marketing digital, redes sociales, redacción creativa "
                "para blogs y programación de actividades turísticas para Ashotel."
            )},
            {"role": "user", "content": datos.mensaje}
        ]
    )
    return {"respuesta": respuesta.choices[0].message.content.strip()}

# --- INICIO routers/generate_image.py v2.4.4‑mt‑refactor ---
"""
routers/generate_image.py

Define el router para generación de imágenes mediante la API OpenAI.
"""

from fastapi import APIRouter, HTTPException
from openai import APIError
import config
from models import ImageRequest, ImageResponse

router = APIRouter(tags=["imágenes"])

@router.post("/generate-image", response_model=ImageResponse)
async def generate_image(request: ImageRequest):
    """
    Genera hasta n imágenes usando la API de OpenAI y devuelve sus URLs.
    """
    if not config.client:
        config.logger.error("Servicio de generación de imágenes no disponible.")
        raise HTTPException(status_code=503, detail="Servicio de generación de imágenes no disponible.")
    try:
        response = config.client.images.generate(
            prompt=request.prompt,
            n=request.n,
            size=request.size
        )
        urls = [item.url for item in response.data]
        return ImageResponse(images=urls)
    except APIError as e:
        config.logger.error(f"Error OpenAI imágenes: {e}", exc_info=True)
        raise HTTPException(status_code=502, detail=f"Error generando imagen: {e.message}")
    except Exception as e:
        config.logger.error(f"Error interno generando imagen: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno generando imagen.")
# --- FIN routers/generate_image.py ---

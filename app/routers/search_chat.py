# app/routers/search_chat.py

"""
search_chat.py - Router FastAPI para búsqueda web integrada con OpenAI

Versión: 1.0.12 (2025‑04‑22)

Changelog:
 1.0.12 (2025‑04‑22)
    - Extracción de PDFs: descarga completa si <30 MB, partial‑fetch de primeros 2 MB si >30 MB.
    - Filtrado de páginas clave por palabras clave (“resumen”, “resultados”, “conclusiones”, “tabla”, “gráfico”, “análisis”).
    - Fallback a primeras 5 páginas si no se encuentran keywords.
    - Umbral PDF aumentado a 30 MB.
 1.0.11 (2025‑04‑22)
    - Partial‑fetch de PDFs grandes con rango HTTP.
    - Mantiene snippets y continúa en caso de error para evitar bloqueos.
 1.0.10 (2025‑04‑22)
    - Timeout global en extracción concurrente (30 s).
 1.0.9… (ver versiones anteriores en commits previos)
"""

import os
import io
import asyncio
import logging
from typing import List
from urllib.parse import quote_plus

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
import httpx
import feedparser
from dotenv import load_dotenv
import openai
from newspaper import Article
from bs4 import BeautifulSoup
import trafilatura
from PyPDF2 import PdfReader

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 1) Carga de variables de entorno (.env)
# ------------------------------------------------------------
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID  = os.getenv("GOOGLE_CSE_ID", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL   = os.getenv("OPENAI_MODEL", "gpt-4o")
for var,name in [
    (GOOGLE_API_KEY,"GOOGLE_API_KEY"),
    (GOOGLE_CSE_ID, "GOOGLE_CSE_ID"),
    (OPENAI_API_KEY,"OPENAI_API_KEY")
]:
    if not var:
        raise RuntimeError(f"{name} no configurada en el entorno.")
openai.api_key = OPENAI_API_KEY

# ------------------------------------------------------------
# 2) Modelos Pydantic
# ------------------------------------------------------------
class SearchChatRequest(BaseModel):
    message:     str  = Field(..., description="Consulta del usuario")
    num_results: int  = Field(2, ge=1, le=5, description="Número de resultados web a incluir")
    debug:       bool = Field(False, description="Si true, devuelve bloques de contexto detallados")

class SearchChatResponse(BaseModel):
    response:       str       = Field(..., description="Respuesta generada por IA")
    context_blocks: List[str] = Field(None, description="Bloques de contexto web (solo si debug=true)")

# ------------------------------------------------------------
# 3) Router con prefijo /agenteiademo
# ------------------------------------------------------------
router = APIRouter(prefix="/agenteiademo", tags=["SearchChat"])

# ------------------------------------------------------------
# 4) Refinar query con OpenAI (SEO) y strip de comillas
# ------------------------------------------------------------
async def refine_query(query: str) -> str:
    seo_prompt = (
        "Eres un experto en SEO. Reformula esta consulta para buscar "
        "noticias precisas en Google:\n\n" + query
    )
    resp = openai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":seo_prompt},
            {"role":"user",  "content":query}
        ]
    )
    refined = resp.choices[0].message.content.strip()
    refined = refined.strip('"\' ')
    logger.info("Query refinada: %s", refined)
    return refined

# ------------------------------------------------------------
# 5) Google Custom Search API + filtrado básico por URL
# ------------------------------------------------------------
async def google_search(query: str, num_results: int) -> List[dict]:
    endpoint = "https://www.googleapis.com/customsearch/v1"
    params   = {"key":GOOGLE_API_KEY, "cx":GOOGLE_CSE_ID, "q":query, "num":num_results}
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(endpoint, params=params)
    if r.status_code != 200:
        logger.error("Google CSE error %d: %s", r.status_code, r.text)
        raise HTTPException(502, f"Google CSE: {r.text}")
    items = r.json().get("items", [])
    def es_noticia(link: str) -> bool:
        return (
            "/2025/" in link or
            link.split("?",1)[0].lower().endswith(".html") or
            any(dom in link for dom in ["canarias7.es","rtve.es","boe.es"])
        )
    filtered = [it for it in items if es_noticia(it.get("link",""))]
    use      = filtered or items
    logger.info("URLs tras filtrado: %d de %d", len(use), len(items))
    return [
        {"title": it["title"], "url": it["link"], "snippet": it.get("snippet","")}
        for it in use
    ]

# ------------------------------------------------------------
# 6) Google News RSS como fallback
# ------------------------------------------------------------
async def fetch_news_rss(query: str, num_results: int) -> List[dict]:
    rss_url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=es&gl=ES&ceid=ES:es"
    async with httpx.AsyncClient(timeout=5.0) as client:
        r = await client.get(rss_url)
    if r.status_code != 200:
        logger.warning("RSS News falla HTTP %d", r.status_code)
        return []
    feed = feedparser.parse(r.text)
    return [
        {
            "title": entry.get("title",""),
            "url": entry.get("link",""),
            "snippet": BeautifulSoup(entry.get("description",""), "lxml").get_text()
        }
        for entry in feed.entries[:num_results]
    ]

# ------------------------------------------------------------
# 7) Extracción de texto con lógica mejorada para PDFs grandes
# ------------------------------------------------------------
async def extract_text(url: str, max_chars: int = 5000) -> str:
    base = url.split("?",1)[0].lower()
    # 7.1) PDF
    if base.endswith(".pdf"):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                head = await client.head(url, follow_redirects=True)
                size = int(head.headers.get("content-length","0"))
                # Si <30MB, descargar completo; si no, descargar primeros 2MB
                if size <= 30_000_000:
                    r = await client.get(url)
                else:
                    r = await client.get(url, headers={"Range":"bytes=0-2097151"})
            reader = PdfReader(io.BytesIO(r.content))
            # 7.1.1) Filtrar páginas clave por keywords
            keywords = ("resumen","resultados","conclusiones","tabla","gráfico","análisis")
            relevant = []
            for page in reader.pages:
                text = page.extract_text() or ""
                if any(kw in text.lower() for kw in keywords):
                    relevant.append(text)
            # 7.1.2) Fallback a primeras 5 páginas si no encontró keywords
            if not relevant:
                relevant = [
                    reader.pages[i].extract_text() or ""
                    for i in range(min(5, len(reader.pages)))
                ]
            combined = "\n\n".join(relevant)
            return combined[:max_chars] + ("…" if len(combined)>max_chars else "")
        except Exception as e:
            logger.warning("PDF extractor falló en %s: %s", url, e)

    # 7.2) trafilatura para HTML
    try:
        raw = trafilatura.fetch_url(url)
        txt = trafilatura.extract(raw) or ""
        if len(txt) > 200:
            return txt[:max_chars] + ("…" if len(txt)>max_chars else "")
    except Exception as e:
        logger.warning("Trafilatura falló en %s: %s", url, e)

    # 7.3) newspaper3k
    try:
        art = Article(url); art.download(); art.parse()
        txt = art.text or ""
        if len(txt) > 200:
            return txt[:max_chars] + ("…" if len(txt)>max_chars else "")
    except Exception as e:
        logger.warning("newspaper3k falló en %s: %s", url, e)

    # 7.4) Meta‑description / readability
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
        soup = BeautifulSoup(r.text[:4096], "lxml")
        tag = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"})
        if tag and tag.get("content"):
            desc = tag["content"]
            return desc[:max_chars] + ("…" if len(desc)>max_chars else "")
        # readability
        from readability import Document
        doc = Document(r.text)
        summary = BeautifulSoup(doc.summary(), "lxml").get_text(separator="\n", strip=True)
        return summary[:max_chars] + ("…" if len(summary)>max_chars else "")
    except Exception as e:
        logger.warning("Fallback extract_text falló en %s: %s", url, e)

    # Fallback final: snippet vacío o muy corto
    return ""

# ------------------------------------------------------------
# 8) Llamada a OpenAI con contexto web integrado
# ------------------------------------------------------------
async def call_openai_with_context(user_msg: str, context_blocks: List[str]) -> str:
    system = (
        "Eres un asistente IA profesional. A continuación, información relevante obtenida de la web:\n\n"
        + "\n---\n".join(context_blocks)
    )
    resp = openai.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[
            {"role":"system","content":system},
            {"role":"user",  "content":user_msg}
        ]
    )
    return resp.choices[0].message.content.strip()

# ------------------------------------------------------------
# 9) Endpoint POST /search-chat
# ------------------------------------------------------------
@router.post("/search-chat", response_model=SearchChatResponse)
async def search_chat(req: SearchChatRequest):
    # 9.1) Refinar consulta
    refined = await refine_query(req.message)

    # 9.2) Obtener resultados CSE + RSS
    cse = await google_search(refined, req.num_results)
    rss = await fetch_news_rss(refined, req.num_results)

    # 9.3) Combinar sin duplicados
    seen, combined = set(), []
    for it in cse + rss:
        if it["url"] not in seen:
            combined.append(it)
            seen.add(it["url"])
        if len(combined) >= req.num_results:
            break

    # 9.4) Fallback a OpenAI puro si no hay fuentes
    if not combined:
        logger.warning("No hay fuentes web, fallback a OpenAI sin contexto")
        resp = openai.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role":"system","content":"Eres un asistente IA profesional y experto."},
                {"role":"user",  "content":req.message}
            ]
        )
        return SearchChatResponse(response=resp.choices[0].message.content.strip(), context_blocks=None)

    # 9.5) Extracción concurrente con timeout (30 s)
    tasks = [extract_text(it["url"]) for it in combined]
    try:
        texts = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)
    except asyncio.TimeoutError:
        logger.warning("Timeout en extracción de textos, usando snippets")
        texts = [it["snippet"] for it in combined]

    # 9.6) Construir bloques de contexto
    context_blocks = []
    for idx, it in enumerate(combined, 1):
        txt = texts[idx-1] or it["snippet"]
        block = f"### Resultado {idx}: {it['title']}\n\n{txt}\n\nURL: {it['url']}"
        context_blocks.append(block)

    # 9.7) Llamar a OpenAI con contexto web
    answer = await call_openai_with_context(req.message, context_blocks)
    logger.info("Respuesta final para el cliente: %s", answer)

    # 9.8) Devolver respuesta (y contexto opcional si debug)
    if req.debug:
        return SearchChatResponse(response=answer, context_blocks=context_blocks)
    return SearchChatResponse(response=answer)

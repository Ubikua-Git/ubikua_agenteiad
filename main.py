# Corrección de indentaciones en la función analizar_documento

# ... código previo no modificado ...

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

    # --- Obtener Prompt Personalizado ---
    custom_prompt_text = ""
    conn = None
    if current_user_id and DB_CONFIGURED:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cursor:
                    cursor.execute("SELECT custom_prompt FROM user_settings WHERE user_id = %s", (current_user_id,))
                    result = cursor.fetchone()
                    if result and result.get('custom_prompt'):
                        custom_prompt_text = result['custom_prompt'].strip()
                        logging.info(f"Prompt OK user: {current_user_id}")
            except (Exception, psycopg2.Error) as e:
                logging.error(f"Error BD get prompt user {current_user_id}: {e}")
            finally:
                if conn:
                    conn.close()

    # --- Construir Prompt Final ---
    prompt_especifico = PROMPT_ESPECIALIZACIONES.get(especializacion_lower, PROMPT_ESPECIALIZACIONES["general"])
    system_prompt_parts = [BASE_PROMPT_ANALISIS_DOC, prompt_especifico]
    if custom_prompt_text:
        system_prompt_parts.extend(["\n\n### Instrucciones Adicionales Usuario ###", custom_prompt_text])
    system_prompt = "\n".join(system_prompt_parts)

    informe_html = ""
    messages_payload = []
    IMAGE_MIMES = ["image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"]

    try:
        if content_type in IMAGE_MIMES:
            logging.info(f"Procesando IMAGEN.")
            image_bytes = await file.read()
            base64_image = base64.b64encode(image_bytes).decode('utf-8')
            user_prompt_image = (
                "Analiza la imagen, extrae su texto (OCR), y redacta un informe HTML profesional basado en ese texto."
                " Sigue formato HTML y evita Markdown. Devuelve solo el HTML."
            )
            messages_payload = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": [
                    {"type": "text", "text": user_prompt_image},
                    {"type": "image_url", "image_url": {"url": f"data:{content_type};base64,{base64_image}"}}
                ]}
            ]

        elif extension in ["pdf", "docx", "doc", "txt", "csv"]:
            logging.info(f"Procesando {extension.upper()}.")
            ruta_temporal = os.path.join(TEMP_DIR, f"up_{os.urandom(8).hex()}.{extension}")
            texto_extraido = ""
            temp_file_saved = False
            try:
                with open(ruta_temporal, "wb") as buffer:
                    shutil.copyfileobj(file.file, buffer)
                    temp_file_saved = True
                if extension in ['pdf', 'doc', 'docx']:
                    texto_extraido = extraer_texto_pdf_docx(ruta_temporal, extension)
                elif extension in ['txt', 'csv']:
                    with open(ruta_temporal, 'r', encoding='utf-8', errors='ignore') as f:
                        texto_extraido = f.read()
                        logging.info(f"Texto extraído TXT/CSV (longitud: {len(texto_extraido)}).")
                else:
                    texto_extraido = "[Error: Tipo no procesable aquí]"
            finally:
                if temp_file_saved and os.path.exists(ruta_temporal):
                    try:
                        os.remove(ruta_temporal)
                        logging.info(f"Archivo temporal eliminado: {ruta_temporal}")
                    except OSError as e:
                        logging.error(f"Error al eliminar archivo temporal {ruta_temporal}: {e}")

            if texto_extraido.startswith("[Error"):
                raise ValueError(texto_extraido)
            if not texto_extraido:
                raise ValueError(f"No se extrajo texto del archivo {extension.upper()}.")

            user_prompt_text = (
                f"Redacta un informe HTML profesional basado en texto:\n--- INICIO ---\n{texto_extraido}\n--- FIN ---\n"
                " Sigue formato HTML, evita Markdown. Devuelve solo HTML."
            )
            messages_payload = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt_text}
            ]
        else:
            raise HTTPException(status_code=415, detail=f"Tipo archivo no soportado: {content_type or extension}.")

        if not messages_payload:
            raise HTTPException(status_code=500, detail="Error interno: No payload IA.")

        respuesta_informe = client.chat.completions.create(
            model="gpt-4-turbo",
            messages=messages_payload,
            temperature=0.3,
            max_tokens=2500
        )
        informe_html = respuesta_informe.choices[0].message.content.strip()

    except Exception as e:
        logging.error(f"Error inesperado /analizar: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Error interno servidor.")
    finally:
        await file.close()

    return RespuestaAnalisis(informe=informe_html)
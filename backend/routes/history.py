"""Generation history endpoints."""

import io

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.orm import Session

from .. import config, models
from ..services import export_import, history
from ..app import safe_content_disposition
from ..database import Generation as DBGeneration, VoiceProfile as DBVoiceProfile, get_db

router = APIRouter()


@router.get("/history", response_model=models.HistoryListResponse)
async def list_history(
    profile_id: str | None = None,
    search: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """List generation history with optional filters."""
    query = models.HistoryQuery(
        profile_id=profile_id,
        search=search,
        limit=limit,
        offset=offset,
    )
    return await history.list_generations(query, db)


@router.get("/history/stats")
async def get_stats(db: Session = Depends(get_db)):
    """Get generation statistics."""
    return await history.get_generation_stats(db)


@router.post("/history/import")
async def import_generation(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Import a generation from a ZIP archive."""
    MAX_FILE_SIZE = 50 * 1024 * 1024

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400, detail=f"File too large. Maximum size is {MAX_FILE_SIZE / (1024 * 1024)}MB"
        )

    try:
        result = await export_import.import_generation_from_zip(content, db)
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/history/failed")
async def clear_failed_generations(db: Session = Depends(get_db)):
    """Delete every generation with status='failed'. Used by the UI's 'Clear failed' button (#410)."""
    count = await history.delete_failed_generations(db)
    return {"deleted": count}


@router.delete("/history/all")
async def delete_all_generations(
    confirm: bool = Query(False, description="Precisa ser true para apagar."),
    db: Session = Depends(get_db),
):
    """Apaga TODO o histórico: áudios, versões e blocos de texto longo. Pula o que está em andamento. Exige confirm=true."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirme com ?confirm=true para apagar todo o histórico.")
    return await history.delete_all_generations(db)


@router.get("/history/{generation_id}", response_model=models.HistoryResponse)
async def get_generation(
    generation_id: str,
    db: Session = Depends(get_db),
):
    """Get a generation by ID."""
    result = (
        db.query(DBGeneration, DBVoiceProfile.name.label("profile_name"))
        .join(DBVoiceProfile, DBGeneration.profile_id == DBVoiceProfile.id)
        .filter(DBGeneration.id == generation_id)
        .first()
    )

    if not result:
        raise HTTPException(status_code=404, detail="Generation not found")

    gen, profile_name = result
    return models.HistoryResponse(
        id=gen.id,
        profile_id=gen.profile_id,
        profile_name=profile_name,
        text=gen.text,
        language=gen.language,
        audio_path=gen.audio_path,
        duration=gen.duration,
        seed=gen.seed,
        instruct=gen.instruct,
        engine=gen.engine or "qwen",
        model_size=gen.model_size,
        status=gen.status or "completed",
        error=gen.error,
        is_favorited=bool(gen.is_favorited),
        created_at=gen.created_at,
    )


@router.post("/history/{generation_id}/favorite")
async def toggle_favorite(
    generation_id: str,
    db: Session = Depends(get_db),
):
    """Toggle the favorite status of a generation."""
    gen = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    gen.is_favorited = not gen.is_favorited
    db.commit()
    return {"is_favorited": gen.is_favorited}


@router.delete("/history/{generation_id}")
async def delete_generation(
    generation_id: str,
    incluir_blocos: bool = Query(False, description="Também apaga os blocos de texto longo (jobs/<id>)."),
    db: Session = Depends(get_db),
):
    """Delete a generation."""
    if incluir_blocos:
        gen = db.query(DBGeneration).filter_by(id=generation_id).first()
        if gen is not None and gen.status in history.ACTIVE_STATUSES:
            raise HTTPException(status_code=409, detail="Geração em andamento; espere terminar ou cancele antes de excluir.")
        history.delete_long_job_files(generation_id)
    success = await history.delete_generation(generation_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Generation not found")
    return {"message": "Generation deleted successfully"}


@router.get("/history/{generation_id}/export")
async def export_generation(
    generation_id: str,
    db: Session = Depends(get_db),
):
    """Export a generation as a ZIP archive."""
    generation = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not generation:
        raise HTTPException(status_code=404, detail="Generation not found")

    try:
        zip_bytes = export_import.export_generation_to_zip(generation_id, db)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    safe_text = "".join(c for c in generation.text[:30] if c.isalnum() or c in (" ", "-", "_")).strip()
    if not safe_text:
        safe_text = "generation"
    # Append a short id so exports of similarly-worded generations don't collide
    # on the same filename (the first 30 chars are frequently identical).
    filename = f"generation-{safe_text}-{generation_id[:8]}.voicebox.zip"

    return StreamingResponse(
        io.BytesIO(zip_bytes),
        media_type="application/zip",
        headers={"Content-Disposition": safe_content_disposition("attachment", filename)},
    )


@router.get("/history/{generation_id}/export-audio")
async def export_generation_audio(
    generation_id: str,
    format: str | None = None,
    db: Session = Depends(get_db),
):
    """Export the audio file from a generation (.wav or .mp3, with automatic companion creation)."""
    from ..services import companion_export

    generation = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not generation:
        raise HTTPException(status_code=404, detail="Generation not found")

    if not generation.audio_path:
        raise HTTPException(status_code=404, detail="Generation has no audio file")

    audio_path = config.resolve_storage_path(generation.audio_path)
    if audio_path is None or not audio_path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    safe_text = "".join(c for c in generation.text[:30] if c.isalnum() or c in (" ", "-", "_")).strip()
    if not safe_text:
        safe_text = "generation"

    # Ensure companion MP3 exists on disk in internal storage
    mp3_path = audio_path.with_suffix(".mp3")
    if not mp3_path.is_file():
        companion_export.wav_to_mp3(audio_path, mp3_path)

    # Base filename
    base_name = f"{safe_text}-{generation_id[:8]}"

    if format and format.lower() == "mp3":
        filename_mp3 = f"{base_name}.mp3"
        return FileResponse(
            mp3_path,
            media_type="audio/mpeg",
            headers={"Content-Disposition": safe_content_disposition("attachment", filename_mp3)},
        )

    # Default is WAV
    filename_wav = f"{base_name}.wav"
    # Register export so background companion watcher will instantly copy MP3 when saved
    companion_export.register_export(generation_id, audio_path, filename_wav)

    return FileResponse(
        audio_path,
        media_type="audio/wav",
        headers={"Content-Disposition": safe_content_disposition("attachment", filename_wav)},
    )


@router.get("/history/{generation_id}/export-mp3")
async def export_generation_mp3(
    generation_id: str,
    db: Session = Depends(get_db),
):
    """Explicit endpoint to export only the MP3 audio file from a generation."""
    return await export_generation_audio(generation_id, format="mp3", db=db)

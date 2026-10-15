"""TTS generation endpoints."""

import asyncio
import logging
import re
import shutil
from typing import Optional
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import config, models
from ..services import history, personality, profiles, tts
from ..database import Generation as DBGeneration, VoiceProfile as DBVoiceProfile, get_db
from ..services.generation import run_generation
from ..services import long_job_ops
from ..services import task_queue as task_queue_mod
from ..services.long_jobs import (
    JobConfigConflictError,
    JobLock,
    JobLockedError,
    LongJobError,
    get_job_dir,
    get_jobs_dir,
    inspect_job_state,
    validate_resume_plan,
)
from ..services.task_queue import cancel_generation as cancel_generation_job, enqueue_generation
from ..utils.audio import load_audio
from ..utils.tasks import get_task_manager

logger = logging.getLogger(__name__)

router = APIRouter()

IMPORTED_AUDIO_PROFILE_NAME = "Imported Audio"
IMPORT_AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac", ".webm"}
IMPORT_AUDIO_MAX_BYTES = 200 * 1024 * 1024  # 200 MB


def _get_or_create_import_profile(db: Session) -> DBVoiceProfile:
    """Singleton profile every imported audio clip points at — keeps the
    Generation FK happy without making profile_id nullable across the schema."""
    row = (
        db.query(DBVoiceProfile)
        .filter(DBVoiceProfile.name == IMPORTED_AUDIO_PROFILE_NAME)
        .first()
    )
    if row is not None:
        return row
    row = DBVoiceProfile(
        id=str(uuid.uuid4()),
        name=IMPORTED_AUDIO_PROFILE_NAME,
        description="External audio imported into a story timeline.",
        language="en",
        voice_type="import",
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _resolve_generation_engine(data: models.GenerationRequest, profile) -> str:
    raw_engine = data.engine
    if not raw_engine or str(raw_engine).strip().lower() in ("none", "null", ""):
        engine = getattr(profile, "default_engine", None) or getattr(profile, "preset_engine", None) or "qwen"
    else:
        engine = str(raw_engine).strip()

    if not engine or str(engine).strip().lower() in ("none", "null", ""):
        engine = "qwen"

    preset_engine = getattr(profile, "preset_engine", None)
    voice_type = getattr(profile, "voice_type", None) or "cloned"
    if voice_type == "preset" and preset_engine == "qwen_custom_voice" and engine in ("qwen", "qwen_custom_voice"):
        return "qwen_custom_voice"
    if voice_type == "cloned" and engine in ("qwen_custom_voice", "none", "None", ""):
        return "qwen"
    return engine


INLINE_TAG_MAP = {
    "whisper": "Speak in a whisper",
    "sussurro": "Fale em tom de sussurro",
    "angry": "Speak in an angry, intense tone",
    "raiva": "Fale com raiva e intensidade",
    "happy": "Speak in a cheerful, happy tone",
    "alegre": "Fale com tom alegre e animado",
    "sad": "Speak in a sad, sorrowful tone",
    "triste": "Fale com tom triste e melancólico",
    "excited": "Speak in an enthusiastic, excited tone",
    "animado": "Fale com muito entusiasmo e animação",
    "fear": "Speak in a frightened, trembling voice",
    "medo": "Fale com medo e voz trêmula",
    "calm": "Speak in a calm, soothing voice",
    "calmo": "Fale com voz calma e suave",
    "sarcastic": "Speak with a sarcastic, mocking tone",
    "sarcastico": "Fale com tom sarcástico e irônico",
    "sarcástico": "Fale com tom sarcástico e irônico",
    "epic": "Speak in a deep, epic, dramatic narrator voice",
    "epico": "Fale como um narrador épico e dramático",
    "épico": "Fale como um narrador épico e dramático",
    "shout": "Speak in a loud, shouting voice",
    "grito": "Fale gritando em voz alta",
}


def _extract_inline_instruct(text: str, explicit_instruct: Optional[str]) -> tuple[str, Optional[str]]:
    """Extract inline instruction or style tags from the text prompt.

    Supports:
      - [instruct: Speak in a whispered, suspenseful tone]
      - [estilo: narrador de suspense]
      - [tom: alegre]
      - [emoção: comovido e choroso]
      - [whisper], [sussurro], [angry], [alegre], [sad], [epic], etc.
    Returns:
      (cleaned_text, effective_instruct)
    """
    if explicit_instruct:
        return text, explicit_instruct

    import re

    # 1. Custom instruction tag: [instruct: ...], [estilo: ...], [tom: ...], [emocao: ...]
    match = re.match(
        r"^\s*\[(?:instruct|instrucao|instrução|estilo|tom|emocao|emoção):\s*([^\]]+)\]\s*",
        text,
        flags=re.IGNORECASE,
    )
    if match:
        extracted = match.group(1).strip()
        cleaned = text[match.end():].strip()
        if cleaned:
            return cleaned, extracted

    # 2. Shorthand tags: [whisper], [sussurro], [angry], [happy], etc.
    match_tag = re.match(r"^\s*\[([a-zA-ZáéíóúÁÉÍÓÚãõÃÕçÇ_-]+)\]\s*", text)
    if match_tag:
        tag_key = match_tag.group(1).lower().strip()
        if tag_key in INLINE_TAG_MAP:
            cleaned = text[match_tag.end():].strip()
            if cleaned:
                return cleaned, INLINE_TAG_MAP[tag_key]

    return text, explicit_instruct


@router.post("/generate", response_model=models.GenerationResponse)
async def generate_speech(
    data: models.GenerationRequest,
    db: Session = Depends(get_db),
):
    """Generate speech from text using a voice profile."""
    task_manager = get_task_manager()
    generation_id = str(uuid.uuid4())

    profile = await profiles.get_profile(data.profile_id, db)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    from ..backends import engine_has_model_sizes

    engine = _resolve_generation_engine(data, profile)
    try:
        profiles.validate_profile_engine(profile, engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    model_size = (data.model_size or "1.7B") if engine_has_model_sizes(engine) else None


    text = data.text
    source = "manual"
    if data.personality and getattr(profile, "personality", None):
        try:
            llm_result = await personality.rewrite_as_profile(profile.personality, data.text)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        text = llm_result.text.strip()
        if not text:
            raise HTTPException(status_code=500, detail="LLM produced empty output; nothing to speak.")
        source = "personality_speak"

    text, instruct = _extract_inline_instruct(text, data.instruct)

    generation = await history.create_generation(
        profile_id=data.profile_id,
        text=text,
        language=data.language,
        audio_path="",
        duration=0,
        seed=data.seed,
        db=db,
        instruct=instruct,
        generation_id=generation_id,
        status="generating",
        engine=engine,
        model_size=model_size if engine_has_model_sizes(engine) else None,
        source=source,
    )

    task_manager.start_generation(
        task_id=generation_id,
        profile_id=data.profile_id,
        text=text,
    )

    effects_chain_config = None
    if data.effects_chain is not None:
        effects_chain_config = [e.model_dump() for e in data.effects_chain]
    else:
        import json as _json

        profile_obj = db.query(DBVoiceProfile).filter_by(id=data.profile_id).first()
        if profile_obj and profile_obj.effects_chain:
            try:
                effects_chain_config = _json.loads(profile_obj.effects_chain)
            except Exception:
                pass

    enqueue_generation(
        generation_id,
        run_generation(
            generation_id=generation_id,
            profile_id=data.profile_id,
            text=text,
            language=data.language,
            engine=engine,
            model_size=model_size,
            seed=data.seed,
            normalize=data.normalize,
            effects_chain=effects_chain_config,
            instruct=instruct,
            mode="generate",
            max_chunk_chars=data.max_chunk_chars,
            crossfade_ms=data.crossfade_ms,
            # Texto longo v2: sem tamanho explícito no pedido, vale o texto_longo.json (220),
            # não o padrão 800 do pedido. O caminho legado continua recebendo 800.
            max_chunk_chars_explicit="max_chunk_chars" in data.model_fields_set,
            assembly_choice={
                k: getattr(data, k)
                for k in ("pause_oracao_ms", "pause_frase_ms", "pause_paragrafo_ms")
                if getattr(data, k) is not None
            } or None,
            quality_choice={
                nome: bool(getattr(data, campo))
                for campo, nome in (("revisar_whisper", "revisar"), ("refazer_sozinho", "refazer_sozinho"),
                                    ("voz_com_sotaque", "voz_com_sotaque"), ("nivelador_volume", "nivelador_volume"))
                if getattr(data, campo)
            } or None,
        )
    )

    return generation


class RetryOptions(BaseModel):
    """Opções do /retry. Só afetam gerações longas com manifesto em disco."""

    from_scratch: bool = Field(
        default=False,
        description="Texto longo: arquiva os blocos já gerados e recomeça do bloco 1. "
        "Sem isso, o retry retoma só os blocos que faltam.",
    )


@router.post("/generate/{generation_id}/retry", response_model=models.GenerationResponse)
async def retry_generation(
    generation_id: str,
    options: Optional[RetryOptions] = Body(None),
    db: Session = Depends(get_db),
):
    """Retry a failed generation using the same parameters."""
    gen = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")

    if (gen.status or "completed") != "failed":
        raise HTTPException(status_code=400, detail="Only failed generations can be retried")

    data_dir = config.get_data_dir()
    long_job_kwargs: dict = {}
    if long_job_ops.is_valid_job_id(generation_id) and long_job_ops.job_manifest_exists(
        data_dir, generation_id
    ):
        if options is not None and options.from_scratch:
            # Recomeça com a mesma divisão/montagem da geração original.
            old_manifest = (await asyncio.to_thread(inspect_job_state, data_dir, generation_id))["manifest"]
            crossfade_ms, normalize = long_job_ops.get_assembly_options(old_manifest)
            long_job_kwargs = {"crossfade_ms": crossfade_ms, "normalize": normalize}
            max_chunk_chars = (old_manifest.get("generation_parameters") or {}).get("max_chunk_chars")
            if max_chunk_chars is not None:
                long_job_kwargs["max_chunk_chars"] = max_chunk_chars
            try:
                await asyncio.to_thread(long_job_ops.archive_job_dir, data_dir, generation_id)
            except JobLockedError as e:
                raise HTTPException(status_code=409, detail=str(e))
        else:
            state = await asyncio.to_thread(inspect_job_state, data_dir, generation_id)
            if not long_job_ops.is_concluded(state):
                _check_resume_allowed(generation_id, gen, None, db)
            elif state.get("is_locked"):
                raise HTTPException(status_code=409, detail="Trabalho em uso por outra operação.")
            return _enqueue_resume(gen, db)

    gen.status = "generating"
    gen.error = None
    gen.audio_path = ""
    gen.duration = 0
    db.commit()
    db.refresh(gen)

    task_manager = get_task_manager()
    task_manager.start_generation(
        task_id=generation_id,
        profile_id=gen.profile_id,
        text=gen.text,
    )

    enqueue_generation(
        generation_id,
        run_generation(
            generation_id=generation_id,
            profile_id=gen.profile_id,
            text=gen.text,
            language=gen.language,
            engine=gen.engine or "qwen",
            model_size=gen.model_size or "1.7B",
            seed=gen.seed,
            instruct=gen.instruct,
            mode="retry",
            **long_job_kwargs,
        )
    )

    return models.GenerationResponse.model_validate(gen)


@router.post(
    "/generate/{generation_id}/regenerate",
    response_model=models.GenerationResponse,
)
async def regenerate_generation(generation_id: str, db: Session = Depends(get_db)):
    """Re-run TTS with the same parameters and save the result as a new version."""
    gen = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    if (gen.status or "completed") != "completed":
        raise HTTPException(status_code=400, detail="Generation must be completed to regenerate")

    data_dir = config.get_data_dir()
    if long_job_ops.is_valid_job_id(generation_id) and long_job_ops.job_manifest_exists(
        data_dir, generation_id
    ):
        # Novo take do zero: a pasta de blocos anterior é preservada por renomeação.
        try:
            await asyncio.to_thread(long_job_ops.archive_job_dir, data_dir, generation_id)
        except JobLockedError as e:
            raise HTTPException(status_code=409, detail=str(e))

    gen.status = "generating"
    gen.error = None
    db.commit()
    db.refresh(gen)

    task_manager = get_task_manager()
    task_manager.start_generation(
        task_id=generation_id,
        profile_id=gen.profile_id,
        text=gen.text,
    )

    version_id = str(uuid.uuid4())

    enqueue_generation(
        generation_id,
        run_generation(
            generation_id=generation_id,
            profile_id=gen.profile_id,
            text=gen.text,
            language=gen.language,
            engine=gen.engine or "qwen",
            model_size=gen.model_size or "1.7B",
            seed=gen.seed,
            instruct=gen.instruct,
            mode="regenerate",
            version_id=version_id,
        )
    )

    return models.GenerationResponse.model_validate(gen)


@router.post("/generate/{generation_id}/cancel")
async def cancel_generation(generation_id: str, db: Session = Depends(get_db)):
    """Cancel a queued or running generation."""
    gen = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")

    if (gen.status or "completed") not in ("loading_model", "generating"):
        raise HTTPException(status_code=400, detail="Only active generations can be cancelled")

    cancellation_state = cancel_generation_job(generation_id)
    if cancellation_state is None:
        # Row says active but the worker is no longer tracking it — the gen
        # coroutine exited without writing a terminal status (most often a
        # SQLite lock racing with the failed-status write inside the worker's
        # exception handler). Fail the row here so the user can move on.
        task_manager = get_task_manager()
        task_manager.complete_generation(generation_id)
        await history.update_generation_status(
            generation_id=generation_id,
            status="failed",
            db=db,
            error="Generation orphaned by worker",
        )
        return {"message": "Orphaned generation cleared"}

    if cancellation_state == "queued":
        task_manager = get_task_manager()
        task_manager.complete_generation(generation_id)
        await history.update_generation_status(
            generation_id=generation_id,
            status="failed",
            db=db,
            error="Generation cancelled",
        )
        return {"message": "Queued generation cancelled"}

    return {"message": "Generation cancellation requested"}


@router.get("/generate/{generation_id}/status")
async def get_generation_status(generation_id: str, db: Session = Depends(get_db)):
    """SSE endpoint that streams generation status updates."""
    import json

    async def event_stream():
        try:
            while True:
                db.expire_all()
                gen = db.query(DBGeneration).filter_by(id=generation_id).first()
                if not gen:
                    yield f"data: {json.dumps({'status': 'not_found', 'id': generation_id})}\n\n"
                    return

                payload = {
                    "id": gen.id,
                    "status": gen.status or "completed",
                    "duration": gen.duration,
                    "error": gen.error,
                    # Agent-originated sources ("mcp", "rest") skip main-window
                    # autoplay — the floating pill plays those directly.
                    "source": gen.source,
                }
                yield f"data: {json.dumps(payload)}\n\n"

                if (gen.status or "completed") in ("completed", "failed"):
                    return

                await asyncio.sleep(1)
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            logger.debug("SSE client disconnected for generation %s", generation_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/generate/stream")
async def stream_speech(
    data: models.GenerationRequest,
    db: Session = Depends(get_db),
):
    """Generate speech and stream the WAV audio directly without saving to disk."""
    from ..backends import (
        engine_needs_trim,
        engine_retries_runaway,
        ensure_model_cached_or_raise,
        get_tts_backend_for_engine,
        load_engine_model,
    )

    profile = await profiles.get_profile(data.profile_id, db)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    engine = _resolve_generation_engine(data, profile)
    try:
        profiles.validate_profile_engine(profile, engine)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tts_model = get_tts_backend_for_engine(engine)
    model_size = data.model_size or "1.7B"

    await ensure_model_cached_or_raise(engine, model_size)
    await load_engine_model(engine, model_size)

    voice_prompt = await profiles.create_voice_prompt_for_profile(
        data.profile_id,
        db,
        engine=engine,
    )

    from ..utils.chunked_tts import generate_chunked

    trim_fn = None
    runaway_detector = None
    if engine_needs_trim(engine):
        from ..utils.audio import trim_tts_output

        trim_fn = trim_tts_output
    if engine_retries_runaway(engine):
        from ..utils.audio import has_tts_runaway

        runaway_detector = has_tts_runaway

    preview_text, preview_instruct = _extract_inline_instruct(data.text, data.instruct)

    audio, sample_rate = await generate_chunked(
        tts_model,
        preview_text,
        voice_prompt,
        language=data.language,
        seed=data.seed,
        instruct=preview_instruct,
        max_chunk_chars=data.max_chunk_chars,
        crossfade_ms=data.crossfade_ms,
        trim_fn=trim_fn,
        runaway_detector=runaway_detector,
    )

    effects_chain_config = None
    if data.effects_chain is not None:
        effects_chain_config = [e.model_dump() for e in data.effects_chain]
    elif profile.effects_chain:
        import json as _json

        try:
            effects_chain_config = _json.loads(profile.effects_chain)
        except Exception:
            effects_chain_config = None

    if effects_chain_config:
        from ..utils.effects import apply_effects

        audio = apply_effects(audio, sample_rate, effects_chain_config)

    if data.normalize:
        from ..utils.audio import normalize_audio

        audio = normalize_audio(audio)

    wav_bytes = tts.audio_to_wav_bytes(audio, sample_rate)

    async def _wav_stream():
        try:
            chunk_size = 64 * 1024
            for i in range(0, len(wav_bytes), chunk_size):
                yield wav_bytes[i : i + chunk_size]
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            logger.debug("Client disconnected during audio stream")

    return StreamingResponse(
        _wav_stream(),
        media_type="audio/wav",
        headers={"Content-Disposition": 'attachment; filename="speech.wav"'},
    )


@router.post("/generate/import", response_model=models.GenerationResponse)
async def import_audio(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Register an external audio file as a generation row.

    Designed for the story timeline so users can drop in music or other
    non-TTS audio. The row points at a singleton "Imported Audio" profile
    so the existing generation/story plumbing keeps working unchanged."""
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in IMPORT_AUDIO_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported audio format '{suffix}'. Allowed: {sorted(IMPORT_AUDIO_EXTENSIONS)}",
        )

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > IMPORT_AUDIO_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"File exceeds {IMPORT_AUDIO_MAX_BYTES // (1024 * 1024)} MB limit.",
            )
        chunks.append(chunk)
    audio_bytes = b"".join(chunks)
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file.")

    generation_id = str(uuid.uuid4())
    target = config.get_generations_dir() / f"{generation_id}{suffix}"
    target.write_bytes(audio_bytes)

    try:
        audio, sr = load_audio(str(target))
        duration = float(len(audio) / sr) if sr else 0.0
    except Exception as decode_err:
        try:
            target.unlink()
        except OSError:
            pass
        raise HTTPException(
            status_code=400,
            detail=f"Could not decode audio: {decode_err}",
        ) from decode_err

    profile = _get_or_create_import_profile(db)
    display_name = Path(file.filename or "Imported audio").stem or "Imported audio"

    return await history.create_generation(
        profile_id=profile.id,
        text=display_name,
        language="en",
        audio_path=config.to_storage_path(target),
        duration=duration,
        seed=None,
        db=db,
        generation_id=generation_id,
        status="completed",
        engine="import",
        model_size=None,
        source="import",
    )


# ---------------------------------------------------------------------------
# Texto longo (Fase 3b-2): rotas aditivas sobre trabalhos com manifesto.
# job_id == generation_id; idx de bloco é 1-based.
# ---------------------------------------------------------------------------


class ResumeRequest(BaseModel):
    """Campos opcionais conferidos contra o manifesto (409 se divergirem)."""

    text: Optional[str] = None
    engine: Optional[str] = None
    model_size: Optional[str] = None
    profile_id: Optional[str] = None
    language: Optional[str] = None


class BlockRegenerateRequest(BaseModel):
    seed: Optional[int] = Field(None, ge=0, description="Semente do bloco; sem ela, sorteia uma nova.")


class ReassembleRequest(BaseModel):
    """Na Fase 3b só os valores padrão são aceitos; outros dão 422 até a Fase 4."""

    pause_oracao_ms: Optional[int] = None
    pause_frase_ms: Optional[int] = None
    pause_paragrafo_ms: Optional[int] = None
    target_lufs: Optional[float] = None
    # Fase 7: liga/desliga o nivelador de volume nesta remontagem (antes e depois para ouvir).
    nivelador_volume: Optional[bool] = None


def _require_job_id(generation_id: str) -> None:
    if not long_job_ops.is_valid_job_id(generation_id):
        raise HTTPException(status_code=400, detail="Identificador de geração inválido.")


def _require_job_state(generation_id: str) -> dict:
    _require_job_id(generation_id)
    data_dir = config.get_data_dir()
    if not long_job_ops.job_manifest_exists(data_dir, generation_id):
        raise HTTPException(status_code=404, detail="Trabalho longo não encontrado.")
    return inspect_job_state(data_dir, generation_id)


def _require_generation_row(generation_id: str, db: Session) -> DBGeneration:
    gen = db.query(DBGeneration).filter_by(id=generation_id).first()
    if not gen:
        raise HTTPException(status_code=404, detail="Generation not found")
    if (gen.status or "completed") in long_job_ops.ACTIVE_DB_STATUSES:
        raise HTTPException(status_code=409, detail="A geração está em andamento ou na fila.")
    return gen


def _require_block(state: dict, idx: int) -> dict:
    chunks = state["manifest"].get("chunks", [])
    if idx < 1 or idx > len(chunks):
        raise HTTPException(status_code=404, detail=f"Bloco {idx} não existe (válidos: 1 a {len(chunks)}).")
    return next(c for c in chunks if c["idx"] == idx)


def _check_resume_allowed(
    generation_id: str, gen: DBGeneration, request_data: Optional[dict], db: Session
) -> None:
    from ..services.long_generation import compute_reference_audio_hash

    data_dir = config.get_data_dir()
    # validate_resume_plan só olha a trava quando o status é "em_andamento";
    # um job "falhou"/"cancelado" com a trava ocupada também não pode retomar.
    if JobLock.is_locked(data_dir, generation_id):
        raise HTTPException(status_code=409, detail="Trabalho em uso por outra operação.")
    try:
        manifest, _pending = validate_resume_plan(data_dir, generation_id, request_data)
    except (JobLockedError, JobConfigConflictError, LongJobError) as e:
        raise HTTPException(status_code=409, detail=str(e))

    voice = manifest.get("voice") or {}
    engine = (manifest.get("model") or {}).get("engine") or gen.engine or "qwen"
    current_hash = compute_reference_audio_hash(voice.get("profile_id") or gen.profile_id, engine, db)
    if current_hash != voice.get("ref_audio_hash_sha256"):
        raise HTTPException(
            status_code=409,
            detail="O áudio de referência da voz mudou desde o início do trabalho; use retry com from_scratch.",
        )


def _enqueue_resume(gen: DBGeneration, db: Session) -> models.GenerationResponse:
    generation_id = gen.id
    # Um cancelamento antigo não pode cancelar a retomada nova.
    task_queue_mod.remove_cancellation_event(generation_id)
    gen.status = "generating"
    gen.error = None
    db.commit()
    db.refresh(gen)

    get_task_manager().start_generation(task_id=generation_id, profile_id=gen.profile_id, text=gen.text)
    enqueue_generation(
        generation_id,
        long_job_ops.run_resume(
            generation_id=generation_id,
            profile_id=gen.profile_id,
            text=gen.text,
            language=gen.language,
            engine=gen.engine or "qwen",
            model_size=gen.model_size,
            seed=gen.seed,
            instruct=gen.instruct,
        ),
    )
    return models.GenerationResponse.model_validate(gen)


@router.get("/generate/{generation_id}/progress")
async def get_long_job_progress(generation_id: str):
    """Progresso de um trabalho longo, derivado do manifesto, do pulso e da trava."""
    state = await asyncio.to_thread(_require_job_state, generation_id)
    manifest = state["manifest"]
    chunks = manifest.get("chunks", [])
    total_chars = sum(c.get("chars", 0) for c in chunks) or 1
    done_chars = sum(c.get("chars", 0) for c in chunks if c.get("status") == "ok")
    heartbeat = state.get("heartbeat") or {}
    return {
        "generation_id": generation_id,
        "job_status": state["declared_status"],
        "effective_status": state["effective_status"],
        "is_locked": state["is_locked"],
        "can_resume": state["can_resume"],
        "warning": state["warning"],
        "completed_chunks": sum(1 for c in chunks if c.get("status") == "ok"),
        "failed_chunks": sum(1 for c in chunks if c.get("status") == "falhou"),
        "total_chunks": len(chunks),
        "percent": round(100.0 * done_chars / total_chars, 1),
        "current_chunk": heartbeat.get("current_chunk"),
        "last_beat": heartbeat.get("last_beat"),
        "master_audio_path": manifest.get("master_audio_path"),
        "error": manifest.get("error"),
    }


@router.get("/generate/{generation_id}/blocks")
async def list_long_job_blocks(generation_id: str):
    """Lista os blocos (1..N) com os campos do manifesto."""
    state = await asyncio.to_thread(_require_job_state, generation_id)
    chunks = state["manifest"].get("chunks", [])
    return {
        "generation_id": generation_id,
        "job_status": state["effective_status"],
        "total_chunks": len(chunks),
        "blocks": chunks,
    }


@router.get("/generate/{generation_id}/blocks/{idx}/audio")
async def get_long_job_block_audio(generation_id: str, idx: int):
    """Áudio WAV (float32) de um bloco já gerado."""
    state = await asyncio.to_thread(_require_job_state, generation_id)
    chunk = _require_block(state, idx)
    if chunk.get("status") != "ok":
        raise HTTPException(status_code=404, detail=f"Bloco {idx} ainda não tem áudio.")
    job_dir = get_job_dir(config.get_data_dir(), generation_id)
    path = long_job_ops.resolve_inside(job_dir, chunk.get("wav_rel_path") or "")
    if path is None:
        raise HTTPException(status_code=403, detail="Caminho do bloco fora da pasta do trabalho.")
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"Arquivo do bloco {idx} não encontrado.")
    return FileResponse(path, media_type="audio/wav", filename=f"{generation_id}_bloco_{idx:02d}.wav")


@router.post(
    "/generate/{generation_id}/blocks/{idx}/regenerate",
    response_model=models.GenerationResponse,
)
async def regenerate_long_job_block(
    generation_id: str,
    idx: int,
    data: Optional[BlockRegenerateRequest] = Body(None),
    db: Session = Depends(get_db),
):
    """Regera só o bloco idx (via fila da GPU) e grava o resultado como novo take."""
    state = await asyncio.to_thread(_require_job_state, generation_id)
    _require_block(state, idx)
    gen = _require_generation_row(generation_id, db)
    if state["is_locked"]:
        raise HTTPException(status_code=409, detail="Trabalho em uso por outra operação.")
    if not long_job_ops.is_concluded(state):
        raise HTTPException(
            status_code=409,
            detail="Só é possível regerar bloco de trabalho concluído; use resume primeiro.",
        )

    task_queue_mod.remove_cancellation_event(generation_id)
    gen.status = "generating"
    gen.error = None
    db.commit()
    db.refresh(gen)

    get_task_manager().start_generation(task_id=generation_id, profile_id=gen.profile_id, text=gen.text)
    enqueue_generation(
        generation_id,
        long_job_ops.run_block_regeneration(
            generation_id=generation_id,
            idx=idx,
            seed=data.seed if data is not None else None,
        ),
    )
    return models.GenerationResponse.model_validate(gen)


@router.post("/generate/{generation_id}/reassemble", response_model=models.GenerationResponse)
async def reassemble_long_job(
    generation_id: str,
    data: Optional[ReassembleRequest] = Body(None),
    db: Session = Depends(get_db),
):
    """Remonta o áudio final a partir dos blocos em disco (só CPU) e grava como novo take."""
    from ..utils.texto_longo_defaults import load_texto_longo_config

    emendas_v2_on = bool(load_texto_longo_config().get("emendas_v2", False))
    overrides = {}
    if data is not None:
        for field, default in long_job_ops.DEFAULT_ASSEMBLY_PARAMS.items():
            value = getattr(data, field)
            if value is None:
                continue
            if emendas_v2_on:
                lo, hi = long_job_ops.ASSEMBLY_PARAM_RANGES[field]
                if not (lo <= float(value) <= hi):
                    raise HTTPException(status_code=422, detail=f"'{field}' fora da faixa {lo} a {hi}.")
                overrides[field] = value
            elif float(value) != float(default):
                raise HTTPException(
                    status_code=422,
                    detail=f"'{field}' diferente do padrão ({default}) exige a opção emendas_v2 ligada.",
                )
        if data.nivelador_volume is not None:
            if not emendas_v2_on:
                raise HTTPException(status_code=422, detail="O nivelador de volume exige a opção emendas_v2 ligada.")
            overrides["nivelador_volume"] = bool(data.nivelador_volume)

    state = await asyncio.to_thread(_require_job_state, generation_id)
    gen = _require_generation_row(generation_id, db)
    if not long_job_ops.is_concluded(state):
        raise HTTPException(status_code=409, detail="Só é possível remontar trabalho concluído.")

    data_dir = config.get_data_dir()
    lock = JobLock(data_dir, generation_id)
    try:
        lock.acquire()
    except JobLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    try:
        await long_job_ops.save_reassembled_take(
            generation_id=generation_id, data_dir=data_dir, db=db, assembly_overrides=overrides or None
        )
    finally:
        lock.release()
    db.refresh(gen)
    return models.GenerationResponse.model_validate(gen)


@router.post("/generate/{generation_id}/resume", response_model=models.GenerationResponse)
async def resume_long_job(
    generation_id: str,
    data: Optional[ResumeRequest] = Body(None),
    db: Session = Depends(get_db),
):
    """Retoma um trabalho longo interrompido, gerando só os blocos que faltam."""
    await asyncio.to_thread(_require_job_state, generation_id)
    gen = _require_generation_row(generation_id, db)
    request_data = data.model_dump(exclude_none=True) if data is not None else None
    _check_resume_allowed(generation_id, gen, request_data or None, db)
    return _enqueue_resume(gen, db)


@router.delete("/generate/{generation_id}/job-files")
async def delete_long_job_files(
    generation_id: str,
    confirm: bool = Query(False, description="Precisa ser true para apagar."),
    db: Session = Depends(get_db),
):
    """Apaga os blocos intermediários de um trabalho CONCLUÍDO para liberar disco.

    O áudio final (em generations/) não é tocado. Exige confirm=true.
    """
    state = await asyncio.to_thread(_require_job_state, generation_id)
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirme com ?confirm=true para apagar os blocos.")
    _require_generation_row(generation_id, db)
    if state["is_locked"]:
        raise HTTPException(status_code=409, detail="Não é permitido apagar arquivos de um trabalho em andamento.")
    if not long_job_ops.is_concluded(state):
        raise HTTPException(status_code=409, detail="Só é possível apagar blocos de trabalho concluído.")
    master = config.resolve_storage_path(state["manifest"].get("master_audio_path"))
    if master is None or not master.is_file():
        raise HTTPException(status_code=409, detail="Áudio final não encontrado; os blocos foram mantidos.")

    data_dir = config.get_data_dir()
    jobs_dir = get_jobs_dir(data_dir).resolve()
    job_dir = get_job_dir(data_dir, generation_id).resolve()
    if job_dir.parent != jobs_dir or job_dir.name != generation_id:
        raise HTTPException(status_code=403, detail="Caminho do trabalho inválido.")

    lock = JobLock(data_dir, generation_id)
    try:
        lock.acquire()
    except JobLockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    try:
        freed = await asyncio.to_thread(long_job_ops._dir_size, job_dir)
        await asyncio.to_thread(shutil.rmtree, job_dir)
    finally:
        lock.release()
    return {"generation_id": generation_id, "deleted": True, "freed_bytes": freed}


@router.get("/system/jobs/storage")
async def get_long_jobs_storage():
    """Espaço em disco ocupado pela pasta jobs/ (somente leitura)."""
    return await asyncio.to_thread(long_job_ops.storage_summary, config.get_data_dir())

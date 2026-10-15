"""
Operações de API sobre trabalhos longos persistidos (Fase 3b-2).

Funções de apoio às rotas aditivas de ``routes/generations.py``:
validação de identificadores, arquivamento de pasta por renomeação,
leitura segura de blocos, remontagem em CPU como novo take, regeração
de um bloco isolado (via fila serial da GPU), retomada, resumo de
armazenamento e reconciliação no startup.

Nada aqui altera o caminho legado de geração curta.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .. import config
from . import history, profiles, task_queue
from .long_jobs import (
    STATUS_CONCLUIDO,
    STATUS_EM_ANDAMENTO,
    STATUS_INTERROMPIDO,
    STATUS_PENDENTE,
    JobLock,
    ManifestManager,
    get_job_dir,
    get_jobs_dir,
    get_manifest_path,
    inspect_job_state,
    read_json_with_retry,
    regenerate_chunk_file,
)

logger = logging.getLogger(__name__)

INTERRUPTED_DB_ERROR = "Interrompido: pode ser retomado"
ARCHIVE_MARKER = "_arquivado_"
ACTIVE_DB_STATUSES = ("generating", "loading_model")

# Valores de remontagem aceitos com emendas_v2 DESLIGADA (o juntador antigo
# não tem pausas nem LUFS): outro valor responde HTTP 422.
DEFAULT_ASSEMBLY_PARAMS = {
    "pause_oracao_ms": 140,
    "pause_frase_ms": 300,
    "pause_paragrafo_ms": 775,
    "target_lufs": -19.0,
}
# Faixas aceitas com emendas_v2 LIGADA (fora delas: HTTP 422).
ASSEMBLY_PARAM_RANGES = {
    "pause_oracao_ms": (0, 2000),
    "pause_frase_ms": (0, 3000),
    "pause_paragrafo_ms": (0, 5000),
    "target_lufs": (-30.0, -9.0),
}


# ---------------------------------------------------------------------------
# Identificadores e caminhos
# ---------------------------------------------------------------------------

def is_valid_job_id(job_id: str) -> bool:
    """Aceita apenas UUID na forma canônica (minúsculas, com hífens)."""
    try:
        return str(uuid.UUID(job_id)) == job_id
    except (ValueError, AttributeError, TypeError):
        return False


def job_manifest_exists(data_dir: Path, job_id: str) -> bool:
    return get_manifest_path(get_job_dir(data_dir, job_id)).exists()


def resolve_inside(base: Path, rel_path: str) -> Optional[Path]:
    """Resolve ``rel_path`` dentro de ``base``; None se escapar da pasta."""
    base_resolved = base.resolve()
    candidate = (base / rel_path).resolve()
    if not candidate.is_relative_to(base_resolved):
        return None
    return candidate


def get_assembly_options(manifest: dict) -> tuple[Optional[int], bool]:
    """Crossfade e normalização usados na montagem original do trabalho.

    Manifestos anteriores à Fase 3b-2 não guardam esses campos: usam os
    padrões de ``GenerationRequest`` (crossfade 50 ms, normalize True).
    """
    params = manifest.get("generation_parameters") or {}
    crossfade_ms = params.get("crossfade_ms")
    normalize = params.get("normalize")
    return (
        int(crossfade_ms) if crossfade_ms is not None else 50,
        bool(normalize) if normalize is not None else True,
    )


# ---------------------------------------------------------------------------
# Arquivamento por renomeação (nada é apagado)
# ---------------------------------------------------------------------------

def archive_job_dir(data_dir: Path, job_id: str) -> Optional[Path]:
    """Renomeia ``jobs/<id>/`` para ``jobs/<id>_arquivado_<timestamp>/``.

    Exige a trava do trabalho: levanta ``JobLockedError`` se ocupada.
    Devolve o novo caminho, ou None se a pasta não existia.
    """
    job_dir = get_job_dir(data_dir, job_id)
    if not job_dir.exists():
        return None

    lock = JobLock(data_dir, job_id)
    lock.acquire()
    try:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        target = get_jobs_dir(data_dir) / f"{job_id}{ARCHIVE_MARKER}{stamp}"
        counter = 1
        while target.exists():
            counter += 1
            target = get_jobs_dir(data_dir) / f"{job_id}{ARCHIVE_MARKER}{stamp}_{counter}"
        for attempt in range(5):
            try:
                job_dir.rename(target)
                return target
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        return target
    finally:
        lock.release()


# ---------------------------------------------------------------------------
# Remontagem em CPU como novo take
# ---------------------------------------------------------------------------

def record_assembly_settings(job_dir: Path, settings: Dict[str, Any], key: str = "last_assembly_settings") -> None:
    """Guarda no manifesto os parâmetros da última remontagem (escrita atômica).

    ``key="pausas_escolhidas"`` guarda a escolha do usuário que a montagem reaplica depois.
    """
    from .long_jobs import write_atomic_json

    mgr = ManifestManager(job_dir)
    with mgr._lock:
        manifest = read_json_with_retry(mgr.manifest_path)
        manifest.setdefault(key, {}).update(settings)
        manifest["timestamps"]["updated_at"] = time.time()
        write_atomic_json(mgr.manifest_path, manifest)


async def save_reassembled_take(
    *, generation_id: str, data_dir: Path, db, assembly_overrides: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Monta o master a partir dos blocos em disco e grava como novo take.

    Quem chama deve segurar a trava do trabalho. Usa o mesmo juntador da
    geração (``assemble_manifest_audio_cpu``) e o mesmo gravador de takes do
    ``/regenerate`` (``_save_regenerate``).
    """
    from ..utils.audio import save_audio
    from .generation import _save_regenerate
    from .long_generation import assemble_manifest_audio_cpu

    job_dir = get_job_dir(data_dir, generation_id)
    mgr = ManifestManager(job_dir)
    manifest = await asyncio.to_thread(mgr.load_manifest)
    crossfade_ms, normalize = get_assembly_options(manifest)

    audio, sample_rate, duration = await asyncio.to_thread(
        assemble_manifest_audio_cpu,
        mgr=mgr,
        job_dir=job_dir,
        crossfade_ms=crossfade_ms,
        normalize=normalize,
        mode="generate",
        assembly_overrides=assembly_overrides,
    )
    if assembly_overrides:
        await asyncio.to_thread(record_assembly_settings, job_dir, assembly_overrides)
        # A remontagem também vira a escolha do trabalho (vale ao regerar bloco depois).
        await asyncio.to_thread(record_assembly_settings, job_dir, assembly_overrides, "pausas_escolhidas")
    final_path = _save_regenerate(
        generation_id=generation_id,
        version_id=None,
        audio=audio,
        sample_rate=sample_rate,
        save_audio=save_audio,
        db=db,
    )
    await history.update_generation_status(
        generation_id=generation_id,
        status="completed",
        db=db,
        audio_path=final_path,
        duration=duration,
    )
    await asyncio.to_thread(mgr.update_master_audio, final_path)
    return {"audio_path": final_path, "duration": duration}


# ---------------------------------------------------------------------------
# Corrotinas enfileiradas na fila serial da GPU
# ---------------------------------------------------------------------------

async def run_block_regeneration(*, generation_id: str, idx: int, seed: Optional[int]) -> None:
    """Regera um único bloco e grava o resultado remontado como novo take.

    O áudio anterior do bloco é preservado por renomeação
    (``chunk_XX.aY.wav``). Se a síntese falhar ou for cancelada, nenhum
    arquivo é tocado e a geração volta a ``completed`` com o áudio anterior.
    """
    from ..backends import (
        engine_needs_trim,
        engine_retries_runaway,
        get_tts_backend_for_engine,
        load_engine_model,
    )
    from ..database import get_db
    from ..utils.audio import has_tts_runaway, trim_tts_output
    from ..utils.chunked_tts import generate_single_chunk
    from ..utils.tasks import get_task_manager
    from .generation import _notify_speak_end

    task_manager = get_task_manager()
    bg_db = next(get_db())
    data_dir = config.get_data_dir()
    job_dir = get_job_dir(data_dir, generation_id)
    lock = JobLock(data_dir, generation_id)
    lock_held = False
    try:
        try:
            lock.acquire()
            lock_held = True
            task_queue.register_long_job(generation_id)
            task_queue.record_task_activity(generation_id)

            mgr = ManifestManager(job_dir)
            manifest = await asyncio.to_thread(mgr.load_manifest)
            chunk = next(c for c in manifest["chunks"] if c["idx"] == idx)
            engine = manifest["model"]["engine"]
            model_size = manifest["model"].get("model_size")
            language = manifest.get("language", "en")
            params = manifest.get("generation_parameters") or {}
            instruct = params.get("instruct")
            profile_id = manifest["voice"]["profile_id"]
            crossfade_ms, _ = get_assembly_options(manifest)
            chunk_seed = seed if seed is not None else random.randint(0, 2**31 - 1)

            tts_model = get_tts_backend_for_engine(engine)
            if not tts_model.is_loaded():
                await history.update_generation_status(generation_id, "loading_model", bg_db)
            await load_engine_model(engine, model_size)
            task_queue.record_task_activity(generation_id)
            voice_prompt = await profiles.create_voice_prompt_for_profile(
                profile_id, bg_db, use_cache=True, engine=engine
            )
            await history.update_generation_status(generation_id, "generating", bg_db)

            t0 = time.time()
            audio, sample_rate = await generate_single_chunk(
                backend=tts_model,
                chunk_text=chunk["text"],
                voice_prompt=voice_prompt,
                language=language,
                chunk_seed=chunk_seed,
                instruct=instruct,
                trim_fn=trim_tts_output if engine_needs_trim(engine) else None,
                runaway_detector=has_tts_runaway if engine_retries_runaway(engine) else None,
                crossfade_ms=crossfade_ms,
            )
            gen_time = time.time() - t0
            task_queue.record_task_activity(generation_id)

            await asyncio.to_thread(
                regenerate_chunk_file,
                job_dir,
                idx,
                audio,
                sample_rate,
                gen_time,
                chunk_seed,
            )
            await save_reassembled_take(generation_id=generation_id, data_dir=data_dir, db=bg_db)
        except asyncio.CancelledError:
            await history.update_generation_status(
                generation_id=generation_id,
                status="completed",
                db=bg_db,
                error=f"Regeração do bloco {idx} cancelada; áudio anterior mantido",
            )
            _notify_speak_end(generation_id, status="cancelled")
            raise
        except Exception as e:
            logger.exception("Falha ao regerar bloco %s do trabalho %s", idx, generation_id)
            await history.update_generation_status(
                generation_id=generation_id,
                status="completed",
                db=bg_db,
                error=f"Falha ao regerar bloco {idx}: {e}; áudio anterior mantido",
            )
            _notify_speak_end(generation_id, status="failed")
        else:
            _notify_speak_end(generation_id, status="completed")
    finally:
        if lock_held:
            lock.release()
        task_manager.complete_generation(generation_id)
        bg_db.close()


async def run_resume(
    *,
    generation_id: str,
    profile_id: str,
    text: str,
    language: str,
    engine: str,
    model_size: Optional[str],
    seed: Optional[int],
    instruct: Optional[str],
) -> None:
    """Retoma um trabalho existente pelo caminho persistente.

    Chama ``run_generation_persistent`` direto (independente das flags),
    porque o manifesto já existe: os blocos vêm dele, nunca do texto
    re-dividido. Modo ``retry``: mesmo arquivo final, sem criar versão.
    """
    from ..database import get_db
    from ..utils.tasks import get_task_manager
    from ..utils.texto_longo_defaults import load_texto_longo_config
    from .long_generation import run_generation_persistent

    data_dir = config.get_data_dir()
    manifest = await asyncio.to_thread(
        read_json_with_retry, get_manifest_path(get_job_dir(data_dir, generation_id))
    )
    crossfade_ms, normalize = get_assembly_options(manifest)

    await run_generation_persistent(
        generation_id=generation_id,
        profile_id=profile_id,
        text=text,
        language=language,
        engine=engine,
        model_size=model_size,
        seed=seed,
        normalize=normalize,
        effects_chain=None,
        instruct=instruct,
        mode="retry",
        max_chunk_chars=None,
        crossfade_ms=crossfade_ms,
        version_id=None,
        chunks_objs=[],
        cfg=load_texto_longo_config(),
        bg_db=next(get_db()),
        task_manager=get_task_manager(),
    )


# ---------------------------------------------------------------------------
# Armazenamento
# ---------------------------------------------------------------------------

def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def storage_summary(data_dir: Path) -> Dict[str, Any]:
    """Resumo do espaço ocupado pela pasta ``jobs/`` (somente leitura)."""
    jobs_dir = get_jobs_dir(data_dir)
    by_status: Dict[str, int] = {}
    active_jobs = 0
    archived_dirs = 0
    without_manifest = 0
    total_bytes = 0
    archived_bytes = 0

    for entry in sorted(jobs_dir.iterdir()):
        if not entry.is_dir():
            try:
                total_bytes += entry.stat().st_size
            except OSError:
                pass
            continue
        size = _dir_size(entry)
        total_bytes += size
        if ARCHIVE_MARKER in entry.name or "_retry_" in entry.name:
            archived_dirs += 1
            archived_bytes += size
            continue
        manifest_path = get_manifest_path(entry)
        if not manifest_path.exists():
            without_manifest += 1
            continue
        active_jobs += 1
        try:
            status = read_json_with_retry(manifest_path).get("job_status", "desconhecido")
        except Exception:
            status = "ilegivel"
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "jobs_dir": str(jobs_dir),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "jobs": active_jobs,
        "jobs_by_status": by_status,
        "archived_dirs": archived_dirs,
        "archived_mb": round(archived_bytes / (1024 * 1024), 2),
        "dirs_without_manifest": without_manifest,
    }


# ---------------------------------------------------------------------------
# Reconciliação no startup
# ---------------------------------------------------------------------------

def reconcile_long_jobs_on_startup(db, data_dir: Path) -> int:
    """Marca trabalhos longos órfãos como retomáveis.

    Roda ANTES do UPDATE genérico de ``app.py``. Para cada geração ativa
    com ``jobs/<id>/manifest.json``: manifesto vai para ``interrompido`` e
    a linha do banco para ``failed`` + "Interrompido: pode ser retomado".
    Gerações sem manifesto ficam para o UPDATE genérico.
    """
    from ..database import Generation as DBGeneration

    count = 0
    rows = db.query(DBGeneration).filter(DBGeneration.status.in_(ACTIVE_DB_STATUSES)).all()
    for gen in rows:
        if not is_valid_job_id(gen.id) or not job_manifest_exists(data_dir, gen.id):
            continue
        try:
            state = inspect_job_state(data_dir, gen.id)
            if state.get("is_locked"):
                logger.warning("Trabalho %s com trava ocupada no startup; não reconciliado.", gen.id)
                continue
            if state.get("declared_status") in (STATUS_PENDENTE, STATUS_EM_ANDAMENTO):
                ManifestManager(get_job_dir(data_dir, gen.id)).update_status(
                    STATUS_INTERROMPIDO, "Servidor reiniciado durante a geração"
                )
            gen.status = "failed"
            gen.error = INTERRUPTED_DB_ERROR
            count += 1
        except Exception as e:
            logger.warning("Falha ao reconciliar trabalho longo %s: %s", gen.id, e)
    # A sessão usa autoflush=False: grava já, para o UPDATE genérico enxergar.
    db.flush()
    return count


def is_concluded(state: Dict[str, Any]) -> bool:
    manifest = state.get("manifest") or {}
    return (
        state.get("effective_status") == STATUS_CONCLUIDO
        and bool(manifest.get("chunks"))
        and all(c.get("status") == "ok" for c in manifest["chunks"])
    )

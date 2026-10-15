"""
Serial generation queue — ensures only one TTS inference runs at a time
to avoid GPU contention.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Coroutine, Literal

from . import gpu_arbitro  # árbitro da GPU (OV-2); carregado já, para não atrasar a 1ª geração

logger = logging.getLogger("voicebox.task_queue")

# Keep references to fire-and-forget background tasks to prevent GC
_background_tasks: set = set()


@dataclass
class GenerationJob:
    """Queued generation work plus the generation ID it belongs to."""

    generation_id: str
    coro: Coroutine


# Generation queue — serializes TTS inference to avoid GPU contention
_generation_queue: asyncio.Queue = None  # type: ignore  # initialized at startup
_generation_worker_task: asyncio.Task | None = None
_queued_generation_ids: set[str] = set()
_running_generation_tasks: dict[str, asyncio.Task] = {}
_cancelled_generation_ids: set[str] = set()

# Eventos de cancelamento cooperativo para threads e corrotinas (Condição C1)
_cancellation_events: dict[str, threading.Event] = {}
_cancellation_lock = threading.Lock()

# Rastreamento de inatividade e batimento cardíaco por tarefa (Condição C8)
_task_last_activity: dict[str, float] = {}
_activity_lock = threading.Lock()

# Rastreamento de tipo de trabalho (curto vs longo) e sinalização de watchdog
_long_jobs: set[str] = set()
_watchdog_interrupted_jobs: set[str] = set()
_long_jobs_lock = threading.Lock()


def get_cancellation_event(generation_id: str) -> threading.Event:
    """Retorna (ou inicializa) o evento thread-safe de cancelamento da geração."""
    with _cancellation_lock:
        if generation_id not in _cancellation_events:
            _cancellation_events[generation_id] = threading.Event()
        return _cancellation_events[generation_id]


def remove_cancellation_event(generation_id: str) -> None:
    """Remove o evento de cancelamento da memória após conclusão da tarefa."""
    with _cancellation_lock:
        _cancellation_events.pop(generation_id, None)


def record_task_activity(generation_id: str) -> None:
    """Registra marca de tempo de atividade (progresso/bloco) de forma thread-safe (C8)."""
    with _activity_lock:
        _task_last_activity[generation_id] = time.time()


def get_task_last_activity(generation_id: str) -> float | None:
    """Obtém a última marca de atividade da geração de forma thread-safe."""
    with _activity_lock:
        return _task_last_activity.get(generation_id)


def remove_task_activity(generation_id: str) -> None:
    """Remove o registro de atividade da geração da memória."""
    with _activity_lock:
        _task_last_activity.pop(generation_id, None)


def register_long_job(generation_id: str) -> None:
    """Marca um trabalho como longo para aplicação de watchdog de inatividade por bloco."""
    with _long_jobs_lock:
        _long_jobs.add(generation_id)


def is_long_job(generation_id: str) -> bool:
    """Verifica se um trabalho está registrado como job longo."""
    with _long_jobs_lock:
        return generation_id in _long_jobs


def mark_watchdog_interrupted(generation_id: str) -> None:
    """Sinaliza que o cancelamento da tarefa foi disparado pelo watchdog de inatividade."""
    with _long_jobs_lock:
        _watchdog_interrupted_jobs.add(generation_id)


def is_watchdog_interrupted(generation_id: str) -> bool:
    """Verifica se a tarefa foi interrompida pelo watchdog de inatividade."""
    with _long_jobs_lock:
        return generation_id in _watchdog_interrupted_jobs


def create_background_task(coro) -> asyncio.Task:
    """Create a background task and prevent it from being garbage collected."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _generation_worker():
    """Worker that processes generation tasks one at a time."""
    while True:
        job = await _generation_queue.get()
        try:
            if job.generation_id in _cancelled_generation_ids:
                _cancelled_generation_ids.discard(job.generation_id)
                get_cancellation_event(job.generation_id).set()

            # Árbitro da GPU (Missão OmniVoice, OV-2): espera o OmniVoice liberar a placa; nunca corta a geração dele.
            if not get_cancellation_event(job.generation_id).is_set() and not gpu_arbitro.geracao_remota(
                job.generation_id
            ):
                try:
                    await gpu_arbitro.pedir_placa(
                        job.generation_id,
                        desistir=lambda gid=job.generation_id: (
                            gid in _cancelled_generation_ids or get_cancellation_event(gid).is_set()
                        ),
                    )
                except Exception as e:  # o árbitro nunca impede uma geração do VozLab
                    logger.warning("Árbitro da GPU falhou (%s); seguindo sem ele.", e)
                # cancelada enquanto esperava a placa: mesmo tratamento de uma cancelada na fila
                if job.generation_id in _cancelled_generation_ids:
                    _cancelled_generation_ids.discard(job.generation_id)
                    get_cancellation_event(job.generation_id).set()

            task = asyncio.create_task(job.coro)
            _running_generation_tasks[job.generation_id] = task
            _queued_generation_ids.discard(job.generation_id)
            record_task_activity(job.generation_id)

            # Carrega parâmetros de timeout e watchdog configuráveis (C4)
            idle_timeout_per_chunk = 600.0
            short_job_timeout = 3600.0
            thread_join_timeout = 60.0
            try:
                from ..utils.texto_longo_defaults import load_texto_longo_config
                cfg = load_texto_longo_config()
                watchdog_cfg = cfg.get("watchdog", {})
                idle_timeout_per_chunk = float(
                    watchdog_cfg.get("idle_timeout_per_chunk_s", cfg.get("idle_timeout_per_chunk_s", 600.0))
                )
                short_job_timeout = float(watchdog_cfg.get("short_job_timeout_s", 3600.0))
                thread_join_timeout = float(watchdog_cfg.get("thread_join_timeout_s", 60.0))
            except Exception:
                pass

            start_time = time.time()
            try:
                while not task.done():
                    done, _ = await asyncio.wait([task], timeout=1.0)
                    if done:
                        break

                    if is_long_job(job.generation_id):
                        last_act = get_task_last_activity(job.generation_id) or start_time
                        idle = time.time() - last_act
                        if idle > idle_timeout_per_chunk:
                            logger.warning(
                                "Watchdog: job longo %s sem avanço por %.1fs (> limite %.1fs). Interrompendo.",
                                job.generation_id,
                                idle,
                                idle_timeout_per_chunk,
                            )
                            mark_watchdog_interrupted(job.generation_id)
                            cancel_ev = get_cancellation_event(job.generation_id)
                            cancel_ev.set()
                            task.cancel()
                            try:
                                await asyncio.wait_for(asyncio.shield(task), timeout=thread_join_timeout)
                            except Exception:
                                pass
                            break
                    else:
                        elapsed = time.time() - start_time
                        if elapsed > short_job_timeout:
                            logger.warning(
                                "Watchdog: job curto %s excedeu %.1fs. Cancelando.",
                                job.generation_id,
                                short_job_timeout,
                            )
                            task.cancel()
                            try:
                                await asyncio.wait_for(asyncio.shield(task), timeout=thread_join_timeout)
                            except Exception:
                                pass
                            await _force_fail_if_active(
                                job.generation_id,
                                "Tempo limite atingido (geração excedeu 60 min). Fila liberada com segurança.",
                            )
                            break
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
        except Exception:
            traceback.print_exc()
            await _force_fail_if_active(
                job.generation_id,
                "Worker exited without writing terminal status",
            )
        finally:
            _running_generation_tasks.pop(job.generation_id, None)
            _queued_generation_ids.discard(job.generation_id)
            remove_cancellation_event(job.generation_id)
            remove_task_activity(job.generation_id)
            _generation_queue.task_done()


async def _force_fail_if_active(generation_id: str, error: str) -> None:
    """Best-effort recovery — flip an active row to failed if the worker
    bailed before writing a terminal status. Catches the case where the gen
    coroutine's own status-write raised (e.g. SQLite lock contention)."""
    try:
        from ..database import Generation as DBGeneration, get_db
        from . import history

        db = next(get_db())
        try:
            gen = db.query(DBGeneration).filter_by(id=generation_id).first()
            if gen is None:
                return
            if (gen.status or "completed") not in ("loading_model", "generating"):
                return
            await history.update_generation_status(
                generation_id=generation_id,
                status="failed",
                db=db,
                error=error,
            )
        finally:
            db.close()
    except Exception:
        traceback.print_exc()


def enqueue_generation(generation_id: str, coro):
    """Add a generation coroutine to the serial queue."""
    if _generation_queue is None:
        raise RuntimeError("Generation queue has not been initialized")

    _queued_generation_ids.add(generation_id)
    _generation_queue.put_nowait(GenerationJob(generation_id=generation_id, coro=coro))


def cancel_generation(generation_id: str) -> Literal["queued", "running"] | None:
    """Cancel a queued or running generation if it is still active."""
    # Sinaliza o evento de cancelamento cooperativo imediatamente
    cancel_event = get_cancellation_event(generation_id)
    cancel_event.set()

    running_task = _running_generation_tasks.get(generation_id)
    if running_task is not None:
        running_task.cancel()
        return "running"

    if generation_id in _queued_generation_ids:
        _queued_generation_ids.discard(generation_id)
        _cancelled_generation_ids.add(generation_id)
        return "queued"

    return None


def init_queue(force: bool = False):
    """Initialize the generation queue and start the worker.

    Must be called once during application startup (inside a running event loop).
    """
    global _generation_queue, _generation_worker_task
    global _queued_generation_ids, _running_generation_tasks, _cancelled_generation_ids

    if _generation_worker_task is not None and not _generation_worker_task.done():
        if not force:
            return
        # Sinaliza cancelamento para todos os eventos ativos no desligamento/reinício (C1)
        with _cancellation_lock:
            for ev in _cancellation_events.values():
                ev.set()
        _generation_worker_task.cancel()
        for task in list(_running_generation_tasks.values()):
            task.cancel()

    _generation_queue = asyncio.Queue()
    _queued_generation_ids = set()
    _running_generation_tasks = {}
    _cancelled_generation_ids = set()
    _generation_worker_task = create_background_task(_generation_worker())


def signal_server_shutdown() -> None:
    """Sinaliza cancelamento para todos os eventos ativos durante o desligamento do servidor (C1)."""
    with _cancellation_lock:
        for ev in _cancellation_events.values():
            ev.set()
    if _generation_worker_task is not None and not _generation_worker_task.done():
        _generation_worker_task.cancel()
    for task in list(_running_generation_tasks.values()):
        task.cancel()

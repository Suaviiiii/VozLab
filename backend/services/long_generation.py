"""
Orquestração de geração de texto longo com persistência estruturada.

Gerencia o ciclo de vida de jobs de texto longo: criação de manifesto,
verificação de hash de referência de áudio (C6), execução com checkpoint
e retomabilidade (C1/C2), watchdog de inatividade (C4) e montagem final
em CPU dos chunks em áudio master.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Literal, Optional

import numpy as np

from .. import config
from . import gpu_arbitro, history, profiles, task_queue
from .long_jobs import (
    STATUS_CANCELADO,
    STATUS_CONCLUIDO,
    STATUS_INTERROMPIDO,
    JobCancelledError,
    ManifestManager,
    get_job_dir,
    run_job,
)
from ..database import (
    ProfileSample as DBProfileSample,
    VoiceProfile as DBVoiceProfile,
)
from ..utils.audio import has_tts_runaway, normalize_audio, save_audio, trim_tts_output
from ..utils.chunked_tts import concatenate_audio_chunks, generate_single_chunk

logger = logging.getLogger(__name__)


def _make_progress_writer(generation_id: str, engine: str, model_size, text: str):
    """Retorna callback que escreve JSON de progresso para o monitor externo.

    Arquivo: <data_dir>/progress/<generation_id>.json
    ETA = (segundos por caractere até o momento) * caracteres restantes.
    """
    started = time.time()
    progress_dir = config.get_data_dir() / "progress"
    try:
        progress_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None
    path = progress_dir / f"{generation_id}.json"

    def _cb(done: int, total: int, chunk_lens: list[int]):
        elapsed = time.time() - started
        total_chars = max(1, sum(chunk_lens))
        done_chars = sum(chunk_lens[:done])
        eta = None
        if done_chars > 0:
            eta = elapsed / done_chars * (total_chars - done_chars)
        data = {
            "id": generation_id,
            "engine": engine,
            "model_size": model_size,
            "text_preview": text[:80],
            "chunks_done": done,
            "chunks_total": total,
            "percent": round(100.0 * done_chars / total_chars, 1),
            "elapsed_s": round(elapsed, 1),
            "eta_s": round(eta, 1) if eta is not None else None,
            "finished": done >= total,
            "updated_at": time.time(),
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)

    return _cb


def compute_reference_audio_hash(profile_id: str, engine: str, bg_db) -> Optional[str]:
    """Calcula hash SHA-256 das amostras de áudio de referência do perfil (C6).

    - Kokoro ou vozes preset: sem hash (None).
    - Chatterbox: bytes dos arquivos armazenados de amostras ordenados por sample.id asc.
    - F5/Qwen/Luxtts: hash combinando áudio + texto de referência por amostra.
    """
    profile_obj = bg_db.query(DBVoiceProfile).filter_by(id=profile_id).first()
    voice_type = getattr(profile_obj, "voice_type", "cloned") or "cloned"

    if engine == "kokoro" or voice_type == "preset" or getattr(profile_obj, "preset_voice_id", None):
        return None
    elif engine in ("chatterbox", "chatterbox_turbo"):
        samples = (
            bg_db.query(DBProfileSample)
            .filter_by(profile_id=profile_id)
            .order_by(DBProfileSample.id.asc())
            .all()
        )
        item_hashes = []
        for s in samples:
            s_path = config.resolve_storage_path(s.audio_path)
            if s_path.exists():
                item_hashes.append(hashlib.sha256(s_path.read_bytes()).hexdigest())
        if item_hashes:
            return hashlib.sha256("".join(item_hashes).encode("utf-8")).hexdigest()
    elif engine in ("f5_tts", "qwen", "luxtts") or voice_type == "cloned":
        samples = (
            bg_db.query(DBProfileSample)
            .filter_by(profile_id=profile_id)
            .order_by(DBProfileSample.id.asc())
            .all()
        )
        item_hashes = []
        for s in samples:
            s_path = config.resolve_storage_path(s.audio_path)
            audio_h = hashlib.sha256(s_path.read_bytes()).hexdigest() if s_path.exists() else ""
            text_h = hashlib.sha256((s.reference_text or "").strip().encode("utf-8")).hexdigest()
            item_hash = hashlib.sha256(f"{audio_h}:{text_h}".encode("utf-8")).hexdigest()
            item_hashes.append(item_hash)
        if item_hashes:
            return hashlib.sha256("".join(item_hashes).encode("utf-8")).hexdigest()

    return None


def emendas_v2_settings(cfg: dict, overrides: Optional[dict] = None) -> dict:
    """Parâmetros da emenda v2 a partir de texto_longo.json (+ ajustes da rota)."""
    pausas = dict(cfg.get("pausas_ms") or {})
    volume = cfg.get("volume") or {}
    emendas = cfg.get("emendas") or {}
    target_lufs = volume.get("target_lufs", -19.0)
    overrides = overrides or {}
    for key, name in (("pause_oracao_ms", "oracao"), ("pause_frase_ms", "frase"), ("pause_paragrafo_ms", "paragrafo")):
        if overrides.get(key) is not None:
            pausas[name] = overrides[key]
    if overrides.get("target_lufs") is not None:
        target_lufs = overrides["target_lufs"]
    return {
        "pausas_ms": pausas,
        "fade_ms": float(emendas.get("crossfade_ms", 15)),
        "trim_threshold_db": float(emendas.get("trim_threshold_db", -45.0)),
        "trim_margin_ms": float(emendas.get("trim_margin_ms", 40)),
        "target_lufs": float(target_lufs),
        "max_gain_db": float(volume.get("max_gain_db", 8.0)),
        "max_dbtp": float(volume.get("max_dbtp", -1.0)),
        "short_s": float(volume.get("curto_rms_threshold_s", 0.4)),
    }


def assemble_manifest_audio_cpu(
    mgr: ManifestManager,
    job_dir: Path,
    crossfade_ms: Optional[int],
    normalize: bool,
    mode: str,
    assembly_overrides: Optional[dict] = None,
) -> tuple[np.ndarray, int, float]:
    """Realiza montagem em CPU do áudio master a partir dos chunks do manifesto (C2).

    Com a flag ``emendas_v2`` ligada, usa a emenda v2 (pausas por fronteira,
    volume por bloco, LUFS final). ``assembly_overrides`` sobrescreve pausas e
    LUFS (rota /reassemble). Com a flag desligada, nada muda.
    """
    import soundfile as sf

    current_manifest = mgr.load_manifest()
    audio_chunks = []
    sample_rate = None
    for c in current_manifest["chunks"]:
        chunk_wav = job_dir / c["wav_rel_path"]
        c_audio, c_sr = sf.read(str(chunk_wav), dtype="float32")
        audio_chunks.append(c_audio)
        if sample_rate is None:
            sample_rate = c_sr

    from ..utils.texto_longo_defaults import load_texto_longo_config

    cfg = load_texto_longo_config()
    if cfg.get("emendas_v2", False) and len(audio_chunks) >= 2:
        from ..utils.emendas_v2 import assemble_v2

        # Pausas/volume escolhidos para ESTE trabalho (na geração ou numa remontagem anterior)
        # valem também ao retomar, regerar bloco ou remontar; ajustes novos se somam por cima.
        # Atenção: NÃO usar "last_assembly_settings" aqui; o manifesto nasce com valores
        # provisórios antigos nesse campo (desde a Fase 3a).
        saved = current_manifest.get("pausas_escolhidas") or {}
        settings = emendas_v2_settings(cfg, {**saved, **(assembly_overrides or {})})
        # Nivelador de volume (Fase 7): escolha do trabalho; ``assembly_overrides`` pode trocar na remontagem.
        # Ordem: remontagem atual > última remontagem (pausas_escolhidas) > escolha na geração.
        nivelar = (assembly_overrides or {}).get("nivelador_volume")
        if nivelar is None:
            nivelar = saved.get("nivelador_volume")
        if nivelar is None:
            nivelar = (current_manifest.get("qualidade_escolhida") or {}).get("nivelador_volume", False)
        assembled, _report = assemble_v2(
            audio_chunks,
            sample_rate,
            [c.get("boundary_type", "frase") for c in current_manifest["chunks"]],
            nivelar=bool(nivelar),
            **settings,
        )
        return assembled, sample_rate, len(assembled) / sample_rate

    assembled = concatenate_audio_chunks(audio_chunks, sample_rate, crossfade_ms=crossfade_ms or 50)
    if normalize or mode == "regenerate":
        assembled = normalize_audio(assembled)
    duration = len(assembled) / sample_rate
    return assembled, sample_rate, duration


JobCancelledErrorTypes = (asyncio.CancelledError, JobCancelledError)


def _marcar_blocos_para_ouvir(mgr: ManifestManager, revisor) -> None:
    """Fim do trabalho: marca "ouvir" nos blocos fora da curva do próprio trabalho (Fase 7)."""
    from .revisao_whisper import marcar_para_ouvir

    manifest = mgr.load_manifest()
    revisoes = {c["idx"]: dict(c["revisao"]) for c in manifest["chunks"] if c.get("revisao")}
    if not revisoes:
        return
    resumo = marcar_para_ouvir(revisoes, revisor.par)
    resumo.update({
        "blocos_revisados": len(revisoes),
        "refeitos": sorted(i for i, r in revisoes.items() if r.get("refeito")),
        "voz_com_sotaque": revisor.sotaque,
        "refazer_sozinho": revisor.refazer,
    })
    mgr.record_review_summary(resumo, revisoes)


async def run_generation_persistent(
    *,
    generation_id: str,
    profile_id: str,
    text: str,
    language: str,
    engine: str,
    model_size: str,
    seed: Optional[int],
    normalize: bool,
    effects_chain: Optional[list],
    instruct: Optional[str],
    mode: Literal["generate", "retry", "regenerate"],
    max_chunk_chars: Optional[int],
    crossfade_ms: Optional[int],
    version_id: Optional[str],
    chunks_objs: list,
    cfg: dict,
    bg_db,
    task_manager,
    assembly_choice: Optional[dict] = None,
    quality_choice: Optional[dict] = None,
) -> None:
    """Executa a síntese de texto longo persistente com manifesto, watchdog e checkpoint.

    ``assembly_choice``: pausas escolhidas na tela (botões Curtas/Padrão/Longas). Só afeta a
    montagem final em CPU; a geração da voz não muda.

    ``quality_choice``: chaves do modo qualidade (Fase 7): revisar, refazer_sozinho,
    voz_com_sotaque, nivelador_volume. Ficam no manifesto e valem também ao retomar.
    """
    from .generation import (
        _notify_speak_end,
        _save_generate,
        _save_regenerate,
        _save_retry,
    )

    revisor = None
    try:
        data_dir = config.get_data_dir()
        job_dir = get_job_dir(data_dir, generation_id)
        manifest_path = job_dir / "manifest.json"

        task_queue.register_long_job(generation_id)
        task_queue.record_task_activity(generation_id)
        cancel_event = task_queue.get_cancellation_event(generation_id)
        # Igual ao legado: "regenerate" usa semente nova (None) para variar o take.
        manifest_seed = seed if mode != "regenerate" else None

        # C5: Trabalho pendente cancelado antes de começar
        if cancel_event.is_set():
            job_dir.mkdir(parents=True, exist_ok=True)
            mgr = ManifestManager(job_dir)
            if not manifest_path.exists():
                chunks_info = [
                    {
                        "idx": i + 1,
                        "paragraph_idx": getattr(c, "paragraph_idx", 1),
                        "boundary_type": getattr(c, "boundary_type", "frase"),
                        "text": c.text,
                        "sample_rate": 24000,
                    }
                    for i, c in enumerate(chunks_objs)
                ]
                await asyncio.to_thread(
                    mgr.create_initial_manifest,
                    job_id=generation_id,
                    chunks_info=chunks_info,
                    model_info={"engine": engine, "model_size": model_size},
                    voice_info={"profile_id": profile_id, "ref_audio_hash_sha256": None},
                    language=language,
                    original_text=text,
                    generation_params={"base_seed": manifest_seed, "instruct": instruct},
                )
            await asyncio.to_thread(mgr.update_status, STATUS_CANCELADO)
            await history.update_generation_status(
                generation_id=generation_id,
                status="failed",
                db=bg_db,
                error="Generation cancelled",
            )
            _notify_speak_end(generation_id, status="cancelled")
            return
    
        # Importações de backends sob demanda
        from ..backends import (
            engine_needs_trim,
            engine_retries_runaway,
            get_tts_backend_for_engine,
            load_engine_model,
        )
    
        # C6: Hash de referência
        ref_audio_hash_sha256 = compute_reference_audio_hash(profile_id, engine, bg_db)
        profile_obj = bg_db.query(DBVoiceProfile).filter_by(id=profile_id).first()
    
        # Criação do manifesto inicial se não existir
        job_dir.mkdir(parents=True, exist_ok=True)
        mgr = ManifestManager(job_dir)
        if not manifest_path.exists():
            chunks_info = [
                {
                    "idx": i + 1,
                    "paragraph_idx": getattr(c, "paragraph_idx", 1),
                    "boundary_type": getattr(c, "boundary_type", "frase"),
                    "text": c.text,
                    "sample_rate": 24000,
                }
                for i, c in enumerate(chunks_objs)
            ]
            await asyncio.to_thread(
                mgr.create_initial_manifest,
                job_id=generation_id,
                chunks_info=chunks_info,
                model_info={"engine": engine, "model_size": model_size},
                voice_info={
                    "profile_id": profile_id,
                    "voice_name": getattr(profile_obj, "name", "") if profile_obj else "",
                    "ref_audio_hash_sha256": ref_audio_hash_sha256,
                },
                language=language,
                original_text=text,
                generation_params={
                    "base_seed": manifest_seed,
                    "temperature": 0.8,
                    "cfg": 2.5,
                    "instruct": instruct,
                    "normalize": bool(normalize or mode == "regenerate"),
                    "crossfade_ms": crossfade_ms if crossfade_ms is not None else 50,
                    "max_chunk_chars": max_chunk_chars,
                },
            )
            if assembly_choice:
                from .long_job_ops import record_assembly_settings

                await asyncio.to_thread(record_assembly_settings, job_dir, assembly_choice, "pausas_escolhidas")
            if quality_choice:
                from .long_job_ops import record_assembly_settings

                await asyncio.to_thread(record_assembly_settings, job_dir, quality_choice, "qualidade_escolhida")

        # C2: Se todos os blocos já estão "ok", pula inferência
        manifest = await asyncio.to_thread(mgr.load_manifest)
        all_chunks_ok = len(manifest.get("chunks", [])) > 0 and all(c["status"] == "ok" for c in manifest["chunks"])
        master_audio_missing = not manifest.get("master_audio_path")
    
        if not (all_chunks_ok and master_audio_missing):
            tts_model = get_tts_backend_for_engine(engine)
            if not tts_model.is_loaded():
                await history.update_generation_status(generation_id, "loading_model", bg_db)
    
            await load_engine_model(engine, model_size)
            task_queue.record_task_activity(generation_id)
    
            voice_prompt = await profiles.create_voice_prompt_for_profile(
                profile_id,
                bg_db,
                use_cache=True,
                engine=engine,
            )
    
            await history.update_generation_status(generation_id, "generating", bg_db)
            loop = asyncio.get_running_loop()
            trim_fn = trim_tts_output if engine_needs_trim(engine) else None
            runaway_detector = has_tts_runaway if engine_retries_runaway(engine) else None
    
            def _synth(chunk_info: dict, chunk_seed: int | None = None) -> tuple[np.ndarray, int]:
                coro = generate_single_chunk(
                    backend=tts_model,
                    chunk_text=chunk_info["text"],
                    voice_prompt=voice_prompt,
                    language=language,
                    chunk_seed=chunk_seed,
                    instruct=instruct,
                    trim_fn=trim_fn,
                    runaway_detector=runaway_detector,
                    crossfade_ms=crossfade_ms or 50,
                )
                fut = asyncio.run_coroutine_threadsafe(coro, loop)
                if engine not in gpu_arbitro.MOTORES_REMOTOS:
                    return fut.result()
                # OV-5: motor remoto (OmniVoice). O bloco roda no serviço; cancelar no meio do bloco cancela o
                # pedido lá (o motor chama /v2/cancelar) e o bloco fica pendente para retomar. Nos motores locais
                # nada muda: cortar a espera deixaria a placa ocupada por uma geração sem dono.
                while True:
                    try:
                        return fut.result(timeout=0.5)
                    except concurrent.futures.TimeoutError:
                        if cancel_event.is_set():
                            fut.cancel()
                            raise JobCancelledError("cancelado durante o bloco (OmniVoice)")
                    except concurrent.futures.CancelledError:
                        raise JobCancelledError("bloco cancelado no serviço OmniVoice")

            # Modo qualidade (Fase 7): o Whisper ouve cada bloco logo depois de gerado.
            escolha = manifest.get("qualidade_escolhida") or {}
            if escolha.get("revisar"):
                from .revisao_whisper import RevisorBlocos

                revisor = RevisorBlocos(language, escolha, cfg)

            def synth_fn(chunk_info: dict, chunk_seed: int | None = None) -> tuple[np.ndarray, int]:
                audio_b, sr_b = _synth(chunk_info, chunk_seed)
                if revisor is None:
                    return audio_b, sr_b
                try:
                    audio_b, sr_b, semente, revisao = revisor.revisar_bloco(
                        chunk_info["text"], audio_b, sr_b, chunk_seed, lambda s: _synth(chunk_info, s)
                    )
                except JobCancelledErrorTypes:
                    raise
                except Exception as exc:  # a revisão nunca derruba a geração
                    logger.warning("Revisão do bloco %s falhou: %s", chunk_info.get("idx"), exc)
                    return audio_b, sr_b
                chunk_info["_semente_usada"] = semente
                chunk_info["_revisao"] = revisao
                task_queue.record_task_activity(generation_id)
                return audio_b, sr_b
    
            from . import generation as gen_mod
            progress_writer = gen_mod._make_progress_writer(generation_id, engine, model_size, text)
            chunk_lens = [len(c["text"]) for c in manifest["chunks"]]
            total_chunks = len(chunk_lens)
    
            def chunk_completed_hook(chunk_idx: int, chunk_info: dict) -> None:
                task_queue.record_task_activity(generation_id)
                revisao = chunk_info.pop("_revisao", None)
                if revisao is not None:
                    try:
                        mgr.record_chunk_review(chunk_idx, revisao)
                    except Exception as exc:
                        logger.warning("Não foi possível gravar a revisão do bloco %s: %s", chunk_idx, exc)
                if progress_writer:
                    progress_writer(chunk_idx, total_chunks, chunk_lens)
    
            watchdog_cfg = cfg.get("watchdog", {})
            thread_join_timeout = float(watchdog_cfg.get("thread_join_timeout_s", 60.0))
    
            thread_task = asyncio.create_task(
                asyncio.to_thread(
                    run_job,
                    data_dir=data_dir,
                    job_id=generation_id,
                    synth_fn=synth_fn,
                    pulse_interval_s=5.0,
                    is_cancelled_fn=cancel_event.is_set,
                    chunk_completed_hook=chunk_completed_hook,
                )
            )
            try:
                final_manifest = await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                cancel_event.set()
                try:
                    await asyncio.wait_for(thread_task, timeout=thread_join_timeout)
                except Exception as e:
                    logger.error("Thread do run_job não encerrou a tempo após cancelamento: %s", e)
    
                if task_queue.is_watchdog_interrupted(generation_id):
                    await asyncio.to_thread(mgr.update_status, STATUS_INTERROMPIDO, "Inatividade excedeu limite por bloco")
                    await history.update_generation_status(
                        generation_id=generation_id,
                        status="failed",
                        db=bg_db,
                        error="Interrompido: pode ser retomado",
                    )
                else:
                    await asyncio.to_thread(mgr.update_status, STATUS_CANCELADO)
                    await history.update_generation_status(
                        generation_id=generation_id,
                        status="failed",
                        db=bg_db,
                        error="Generation cancelled",
                    )
                _notify_speak_end(generation_id, status="cancelled")
                raise
    
            if revisor is not None:
                revisor.descarregar()
                if final_manifest.get("job_status") == STATUS_CONCLUIDO:
                    await asyncio.to_thread(_marcar_blocos_para_ouvir, mgr, revisor)

            if final_manifest.get("job_status") != STATUS_CONCLUIDO:
                if final_manifest.get("job_status") == STATUS_CANCELADO:
                    await history.update_generation_status(
                        generation_id=generation_id,
                        status="failed",
                        db=bg_db,
                        error="Generation cancelled",
                    )
                    _notify_speak_end(generation_id, status="cancelled")
                    return
                elif final_manifest.get("job_status") == STATUS_INTERROMPIDO:
                    await history.update_generation_status(
                        generation_id=generation_id,
                        status="failed",
                        db=bg_db,
                        error="Interrompido: pode ser retomado",
                    )
                    _notify_speak_end(generation_id, status="failed")
                    return
                else:
                    await history.update_generation_status(
                        generation_id=generation_id,
                        status="failed",
                        db=bg_db,
                        error=final_manifest.get("error", "Falha na geração"),
                    )
                    _notify_speak_end(generation_id, status="failed")
                    return
    
        # C2: Montagem Final em CPU (mesmo juntador e mesma normalização)
        audio, sample_rate, duration = await asyncio.to_thread(
            assemble_manifest_audio_cpu,
            mgr=mgr,
            job_dir=job_dir,
            crossfade_ms=crossfade_ms,
            normalize=normalize,
            mode=mode,
        )
    
        if mode == "generate":
            final_path = _save_generate(
                generation_id=generation_id,
                audio=audio,
                sample_rate=sample_rate,
                effects_chain=effects_chain,
                save_audio=save_audio,
                db=bg_db,
            )
        elif mode == "retry":
            final_path = _save_retry(
                generation_id=generation_id,
                audio=audio,
                sample_rate=sample_rate,
                save_audio=save_audio,
            )
        elif mode == "regenerate":
            final_path = _save_regenerate(
                generation_id=generation_id,
                version_id=version_id,
                audio=audio,
                sample_rate=sample_rate,
                save_audio=save_audio,
                db=bg_db,
            )
    
        await history.update_generation_status(
            generation_id=generation_id,
            status="completed",
            db=bg_db,
            audio_path=final_path,
            duration=duration,
        )
        # Grava master_audio_path no manifesto DEPOIS de salvar no histórico (C2)
        await asyncio.to_thread(mgr.update_master_audio, final_path)
        _notify_speak_end(generation_id, status="completed")

    except Exception as e:
        # Igual ao legado: erro inesperado nunca deixa a linha presa em "generating".
        logger.exception("Falha no trabalho longo %s", generation_id)
        await history.update_generation_status(
            generation_id=generation_id,
            status="failed",
            db=bg_db,
            error=str(e),
        )
        _notify_speak_end(generation_id, status="failed")

    finally:
        if revisor is not None:
            try:
                revisor.descarregar()
            except Exception:
                pass
        task_manager.complete_generation(generation_id)
        bg_db.close()


_run_generation_persistent = run_generation_persistent

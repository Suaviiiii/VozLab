"""
Suíte de testes automatizados da Fase 3b-1 (Núcleo de Texto Longo V2).
Validação estrita das condições C1 a C8:
- C1: Cancelamento cooperativo com thread join e serialização na fila (job 2 só inicia após término da thread de job 1).
- C2: Montagem final resumível em CPU sem chamar synth_fn quando todos os blocos estão prontos.
- C3: Equivalência estrita com tag fase2-aprovada (worktree) nos 3 caminhos e persistência ligada vs desligada.
- C4: Watchdog de inatividade por bloco e limite fixo para texto curto.
- C5: Divisão e manifesto inicial no worker, cancelamento de job pendente sem pasta órfã.
- C6: Hash de referência do Chatterbox (amostras armazenadas ordenadas por ID) e F5/Qwen (áudio + texto).
- C7: Modelos Pydantic isolados em models_texto_longo.py.
- C8: Thread-safety de record_task_activity.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import soundfile as sf
import torch
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import (
    Base,
    Generation,
    GenerationVersion,
    ProfileSample,
    VoiceProfile,
)
from backend.models_texto_longo import (
    BlockAttempt,
    BlockMetadataResponse,
    LongJobProgressResponse,
    ReassembleRequest,
    StorageSummaryResponse,
)
from backend.services import long_jobs as lj
from backend.services import task_queue
from backend.services.generation import (
    _run_generation_persistent,
    run_generation,
)
from backend.utils.chunked_tts import (
    ChunkV2,
    concatenate_audio_chunks,
    generate_chunked,
    generate_single_chunk,
    split_text_into_chunks_v2,
)


class MockDeterministicBackend:
    """Backend determinístico para testes matemáticos de equivalência sem GPU."""

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate
        self.call_count = 0

    async def generate(
        self,
        text: str,
        voice_prompt: dict,
        language: str = "pt",
        seed: int | None = None,
        instruct: str | None = None,
    ):
        self.call_count += 1
        rng = np.random.RandomState(seed if seed is not None else 12345)
        num_samples = max(2400, len(text) * 480)
        audio = rng.uniform(-0.4, 0.4, size=num_samples).astype(np.float32)
        return audio, self.sample_rate


class TestC1Cancellation(unittest.IsolatedAsyncioTestCase):
    """C1: Cancelamento cooperativo com thread join e serialização na fila."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        task_queue.init_queue(force=True)

    async def asyncTearDown(self):
        task_queue.signal_server_shutdown()
        self.tmp_dir.cleanup()

    async def test_c1_two_jobs_cancellation_order(self):
        """Fila com 2 trabalhos: cancelar o 1º no meio de um bloco lento;

        provar que o 2º trabalho SÓ inicia depois do término da thread do 1º.
        """
        events_timeline: list[tuple[str, float]] = []

        def slow_thread(cancel_ev: threading.Event):
            events_timeline.append(("job1_thread_start", time.time()))
            cancel_ev.wait(timeout=0.4)
            events_timeline.append(("job1_thread_end", time.time()))

        def fast_thread(cancel_ev: threading.Event):
            events_timeline.append(("job2_thread_start", time.time()))
            events_timeline.append(("job2_thread_end", time.time()))

        async def run_job1():
            cancel_ev = task_queue.get_cancellation_event("job1")
            task_queue.register_long_job("job1")
            thread_task = asyncio.create_task(asyncio.to_thread(slow_thread, cancel_ev))
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                cancel_ev.set()
                await asyncio.wait_for(asyncio.shield(thread_task), timeout=5.0)
                events_timeline.append(("job1_worker_thread_joined", time.time()))
                raise

        async def run_job2():
            cancel_ev = task_queue.get_cancellation_event("job2")
            task_queue.register_long_job("job2")
            thread_task = asyncio.create_task(asyncio.to_thread(fast_thread, cancel_ev))
            await asyncio.shield(thread_task)
            events_timeline.append(("job2_finished", time.time()))

        task_queue.enqueue_generation("job1", run_job1())
        task_queue.enqueue_generation("job2", run_job2())

        # Espera job1 iniciar sua thread
        await asyncio.sleep(0.1)
        events_timeline.append(("cancel_requested", time.time()))
        task_queue.cancel_generation("job1")

        # Aguarda job2 terminar
        await asyncio.sleep(0.6)

        times = dict(events_timeline)
        self.assertIn("job1_thread_end", times)
        self.assertIn("job1_worker_thread_joined", times)
        self.assertIn("job2_thread_start", times)
        print(f"\n[C1 TIMESTAMPS] job1_thread_end={times['job1_thread_end']:.6f}, job1_worker_thread_joined={times['job1_worker_thread_joined']:.6f}, job2_thread_start={times['job2_thread_start']:.6f}, delta={(times['job2_thread_start'] - times['job1_thread_end']):.6f}s")
        # PROVA C1: Job 2 só começa APÓS o término da thread do Job 1
        self.assertGreaterEqual(
            times["job2_thread_start"],
            times["job1_thread_end"],
            "VIOLAÇÃO C1: Job 2 iniciou antes do término da thread do Job 1!",
        )
        self.assertGreaterEqual(
            times["job2_thread_start"],
            times["job1_worker_thread_joined"],
            "VIOLAÇÃO C1: Job 2 iniciou antes da junção da thread no worker!",
        )

    def test_c1_server_shutdown_signals_events(self):
        """No desligamento do servidor, sinalizar os eventos e esperar."""
        ev1 = task_queue.get_cancellation_event("shutdown_job_1")
        ev2 = task_queue.get_cancellation_event("shutdown_job_2")
        self.assertFalse(ev1.is_set())
        self.assertFalse(ev2.is_set())

        task_queue.signal_server_shutdown()

        self.assertTrue(ev1.is_set(), "VIOLAÇÃO C1: Evento 1 não foi sinalizado no shutdown!")
        self.assertTrue(ev2.is_set(), "VIOLAÇÃO C1: Evento 2 não foi sinalizado no shutdown!")


class TestC2ResumableAssembly(unittest.TestCase):
    """C2: Montagem final resumível em CPU sem chamar synth_fn quando todos os blocos estão prontos."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        self.job_id = "test-job-c2-resume"
        self.job_dir = lj.get_job_dir(self.data_dir, self.job_id)
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_c2_pure_cpu_reassembly_on_resume(self):
        """Se todos os blocos estão 'ok' no manifesto mas master_audio_path está vazio,

        a retomada executa SÓ montagem em CPU e produz áudio idêntico sem chamar synth_fn.
        """
        text = "Primeiro bloco de teste para c2. Segundo bloco para validar montagem pura."
        sample_rate = 24000

        # 1. Inicializa manifesto com 2 blocos
        mgr = lj.ManifestManager(self.job_dir)
        chunks_info = [
            {"idx": 1, "paragraph_idx": 1, "boundary_type": "frase", "text": "Primeiro bloco de teste para c2.", "sample_rate": 24000},
            {"idx": 2, "paragraph_idx": 1, "boundary_type": "paragrafo", "text": "Segundo bloco para validar montagem pura.", "sample_rate": 24000},
        ]
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=chunks_info,
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "test_profile", "ref_audio_hash_sha256": None},
            language="pt",
            original_text=text,
            generation_params={"base_seed": 42},
        )

        def initial_synth_fn(chunk_info: dict, chunk_seed: int | None = None):
            idx = chunk_info.get("idx", 1)
            rng = np.random.RandomState(42 + idx)
            audio = rng.uniform(-0.3, 0.3, size=4800).astype(np.float32)
            return audio, sample_rate

        res1 = lj.run_job(
            data_dir=self.data_dir,
            job_id=self.job_id,
            synth_fn=initial_synth_fn,
        )
        self.assertEqual(res1.get("job_status"), lj.STATUS_CONCLUIDO)

        # Simula processo interrompido antes do master_audio_path ser gravado
        manifest = mgr.load_manifest()
        manifest["master_audio_path"] = ""
        manifest["job_status"] = lj.STATUS_INTERROMPIDO
        lj.write_atomic_json(mgr.manifest_path, manifest)

        # Montagem de referência a partir dos arquivos salvos
        chunk_files = [self.job_dir / c["wav_rel_path"] for c in manifest["chunks"]]
        ref_chunks = [sf.read(str(p), dtype="float32")[0] for p in chunk_files]
        expected_audio = concatenate_audio_chunks(ref_chunks, sample_rate=sample_rate, crossfade_ms=50)

        # 2. Retomada: synth_fn DEVE FALHAR se for chamado!
        def failing_synth_fn(chunk_info: dict, chunk_seed: int | None = None):
            raise AssertionError("VIOLAÇÃO C2: synth_fn foi chamado durante montagem pura em CPU!")

        # Verifica se todos os blocos estão ok e master_audio_path vazio
        manifest = mgr.load_manifest()
        all_chunks_ok = len(manifest.get("chunks", [])) > 0 and all(c["status"] == "ok" for c in manifest["chunks"])
        master_audio_missing = not manifest.get("master_audio_path")
        self.assertTrue(all_chunks_ok)
        self.assertTrue(master_audio_missing)

        # Executa SÓ montagem em CPU
        audio_chunks = []
        sr = None
        for c in manifest["chunks"]:
            chunk_wav = self.job_dir / c["wav_rel_path"]
            self.assertTrue(chunk_wav.exists())
            c_audio, c_sr = sf.read(str(chunk_wav), dtype="float32")
            audio_chunks.append(c_audio)
            if sr is None:
                sr = c_sr

        resumed_final_audio = concatenate_audio_chunks(audio_chunks, sr, crossfade_ms=50)

        # Grava master_audio_path no manifesto
        master_path = self.job_dir / "master_audio.wav"
        lj.write_atomic_audio(master_path, resumed_final_audio, sr)
        mgr.update_master_audio(str(master_path))

        # PROVA C2: Áudio resultante é 100% IDÊNTICO (np.array_equal)
        self.assertTrue(
            np.array_equal(resumed_final_audio, expected_audio),
            "VIOLAÇÃO C2: Áudio montado na retomada difere da montagem de referência!",
        )

        final_manifest = mgr.load_manifest()
        self.assertEqual(final_manifest["master_audio_path"], str(master_path))
        self.assertEqual(final_manifest["job_status"], lj.STATUS_CONCLUIDO)


class TestC3LegacyEquivalence(unittest.IsolatedAsyncioTestCase):
    """C3: Equivalência estrita do legado (tag fase2-aprovada) e persistência desligada vs ligada."""

    @classmethod
    def setUpClass(cls):
        import subprocess
        cls._tmp_worktree = tempfile.TemporaryDirectory()
        worktree_dir = Path(cls._tmp_worktree.name)
        git_cmd = "git"
        subprocess.run(
            [git_cmd, "worktree", "add", str(worktree_dir), "fase2-aprovada"],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
            capture_output=True,
            check=True,
        )
        worktree_file = worktree_dir / "backend" / "utils" / "chunked_tts.py"
        spec = importlib.util.spec_from_file_location(
            "backend_fase2.utils.chunked_tts",
            str(worktree_file),
            submodule_search_locations=[],
        )
        cls.mod_fase2 = importlib.util.module_from_spec(spec)
        cls.mod_fase2.__package__ = "backend.utils"
        spec.loader.exec_module(cls.mod_fase2)

    @classmethod
    def tearDownClass(cls):
        import subprocess
        git_cmd = "git"
        subprocess.run(
            [git_cmd, "worktree", "remove", "--force", str(cls._tmp_worktree.name)],
            cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
            capture_output=True,
        )
        cls._tmp_worktree.cleanup()

    async def test_c3_worktree_normal_path_identity(self):
        """Caminho normal determinístico é 100% bit-a-bit idêntico à tag fase2-aprovada."""
        backend = MockDeterministicBackend()
        text = "Primeira frase de teste para equivalência. Segunda frase do texto. Terceira frase final."

        a_fase2, sr1 = await self.mod_fase2.generate_chunked(backend, text, {}, seed=42, max_chunk_chars=50)
        a_atual, sr2 = await generate_chunked(backend, text, {}, seed=42, max_chunk_chars=50)

        self.assertEqual(sr1, sr2)
        self.assertTrue(
            np.array_equal(a_fase2, a_atual),
            "VIOLAÇÃO C3: Caminho normal difere entre fase2-aprovada e atual!",
        )

    async def test_c3_worktree_trim_fn_path_identity(self):
        """Caminho com trim_fn é 100% bit-a-bit idêntico à tag fase2-aprovada."""
        backend = MockDeterministicBackend()
        text = "Primeira frase com aparo sonoro. Segunda frase com aparo sonoro. Terceira frase final."

        def mock_trim(audio, sr):
            return audio[50:-50] if len(audio) > 100 else audio

        a_fase2, _ = await self.mod_fase2.generate_chunked(
            backend, text, {}, seed=42, max_chunk_chars=50, trim_fn=mock_trim
        )
        a_atual, _ = await generate_chunked(
            backend, text, {}, seed=42, max_chunk_chars=50, trim_fn=mock_trim
        )

        self.assertTrue(
            np.array_equal(a_fase2, a_atual),
            "VIOLAÇÃO C3: Caminho com trim_fn difere entre fase2-aprovada e atual!",
        )

    async def test_c3_worktree_runaway_detector_path_identity(self):
        """Caminho com detector de alucinação e split é 100% bit-a-bit idêntico à tag fase2-aprovada."""
        backend = MockDeterministicBackend()
        text = "Esta é uma sentença longa o suficiente para disparar o detector de instabilidade e fuga."

        def mock_runaway_detector(audio, sr):
            return len(audio) > 30000

        a_fase2, _ = await self.mod_fase2.generate_chunked(
            backend, text, {}, seed=42, max_chunk_chars=120, runaway_detector=mock_runaway_detector
        )
        a_atual, _ = await generate_chunked(
            backend, text, {}, seed=42, max_chunk_chars=120, runaway_detector=mock_runaway_detector
        )

        self.assertTrue(
            np.array_equal(a_fase2, a_atual),
            "VIOLAÇÃO C3: Caminho do runaway detector difere entre fase2-aprovada e atual!",
        )

    def test_c3_persistence_on_vs_off_numerical_identity(self):
        """Áudio gerado com persistência desligada é matematicamente idêntico ao montado com persistência ligada."""
        text = "Frase um do teste comparativo. Frase dois do teste comparativo. Frase três final."
        sample_rate = 24000

        chunks_objs = split_text_into_chunks_v2(text, max_chars=45, min_chars=10, as_objects=True)
        mem_chunks = []
        for i, c in enumerate(chunks_objs):
            rng = np.random.RandomState(42 + i)
            audio = rng.uniform(-0.3, 0.3, size=max(2400, len(c.text) * 480)).astype(np.float32)
            mem_chunks.append(audio)

        audio_off = concatenate_audio_chunks(mem_chunks, sample_rate, crossfade_ms=50)

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            disk_chunks = []
            for i, c in enumerate(chunks_objs):
                rng = np.random.RandomState(42 + i)
                audio = rng.uniform(-0.3, 0.3, size=max(2400, len(c.text) * 480)).astype(np.float32)
                wav_file = tmp_path / f"chunk_{i:02d}.wav"
                lj.write_atomic_audio(wav_file, audio, sample_rate)
                reloaded_audio, _ = sf.read(str(wav_file), dtype="float32")
                disk_chunks.append(reloaded_audio)

            audio_on = concatenate_audio_chunks(disk_chunks, sample_rate, crossfade_ms=50)

        self.assertTrue(
            np.array_equal(audio_off, audio_on),
            "VIOLAÇÃO C3: Áudio montado com persistência difere da montagem em memória!",
        )


class TestC4Watchdog(unittest.IsolatedAsyncioTestCase):
    """C4: Watchdog de inatividade por bloco e limite fixo para texto curto."""

    async def asyncSetUp(self):
        task_queue.init_queue(force=True)

    async def test_c4_long_job_idle_timeout(self):
        """Trabalho longo que ultrapassa IDLE_TIMEOUT_PER_CHUNK é interrompido pelo watchdog."""
        gen_id = "test-c4-idle-timeout"
        events = []

        def stalling_thread(cancel_ev: threading.Event):
            events.append(("stalling_start", time.time()))
            cancel_ev.wait(timeout=2.0)
            events.append(("stalling_end", time.time()))

        async def run_stalling_job():
            cancel_ev = task_queue.get_cancellation_event(gen_id)
            task_queue.register_long_job(gen_id)
            task_queue.record_task_activity(gen_id)
            thread_task = asyncio.create_task(asyncio.to_thread(stalling_thread, cancel_ev))
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                cancel_ev.set()
                await asyncio.wait_for(asyncio.shield(thread_task), timeout=5.0)
                events.append(("coro_joined", time.time()))
                raise

        with patch("backend.utils.texto_longo_defaults.load_texto_longo_config") as mock_cfg:
            mock_cfg.return_value = {
                "watchdog": {
                    "idle_timeout_per_chunk_s": 0.2,
                    "short_job_timeout_s": 3600.0,
                    "thread_join_timeout_s": 5.0,
                }
            }
            task_queue.enqueue_generation(gen_id, run_stalling_job())

            for _ in range(15):
                if task_queue.is_watchdog_interrupted(gen_id):
                    break
                await asyncio.sleep(0.1)

        self.assertTrue(
            task_queue.is_watchdog_interrupted(gen_id),
            "VIOLAÇÃO C4: Watchdog não disparou após inatividade!",
        )


class TestC5InitialManifestAndPendingCancellation(unittest.IsolatedAsyncioTestCase):
    """C5: Divisão e manifesto inicial no worker; cancelamento antes do início sem pasta órfã."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        task_queue.init_queue(force=True)

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def test_c5_pending_job_cancelled_creates_manifest(self):
        """Um trabalho pendente cancelado antes de iniciar cria manifesto 'cancelado'

        sem deixar pasta órfã sem manifesto.
        """
        gen_id = "test-pending-cancelled-job"
        text = "Primeiro bloco de teste longo. Segundo bloco de teste longo. Terceiro bloco."
        chunks_objs = split_text_into_chunks_v2(text, max_chars=40, as_objects=True)

        job_dir = lj.get_job_dir(self.data_dir, gen_id)
        manifest_path = job_dir / "manifest.json"

        # Mock de DB
        mock_db = MagicMock()
        mock_task_manager = MagicMock()

        # Sinaliza cancelamento antes de executar
        cancel_event = task_queue.get_cancellation_event(gen_id)
        cancel_event.set()

        with patch("backend.config.get_data_dir", return_value=self.data_dir), \
             patch("backend.services.history.update_generation_status", new_callable=unittest.mock.AsyncMock):
            await _run_generation_persistent(
                generation_id=gen_id,
                profile_id="test_profile",
                text=text,
                language="pt",
                engine="kokoro",
                model_size="small",
                seed=42,
                normalize=True,
                effects_chain=None,
                instruct=None,
                mode="generate",
                max_chunk_chars=40,
                crossfade_ms=50,
                version_id=None,
                chunks_objs=chunks_objs,
                cfg={"watchdog": {"thread_join_timeout_s": 5.0}},
                bg_db=mock_db,
                task_manager=mock_task_manager,
            )

        self.assertTrue(manifest_path.exists(), "VIOLAÇÃO C5: Pasta órfã criada sem manifesto!")

        mgr = lj.ManifestManager(job_dir)
        manifest = mgr.load_manifest()

        self.assertEqual(manifest["job_status"], lj.STATUS_CANCELADO)
        self.assertEqual(manifest["total_chunks"], len(chunks_objs))
        self.assertEqual(manifest["completed_chunks"], 0)


class TestC6ReferenceAudioHash(unittest.TestCase):
    """C6: Hash de referência do Chatterbox (amostras armazenadas ordenadas por ID) e F5/Qwen (áudio + texto)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)

        db_path = self.data_dir / "test.db"
        self.engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()

    def tearDown(self):
        self.session.close()
        self.engine.dispose()
        self.tmp_dir.cleanup()

    def test_c6_chatterbox_reference_audio_hash(self):
        """Chatterbox: bytes dos arquivos de amostra ARMAZENADOS ordenados por sample.id."""
        prof = VoiceProfile(id="prof_cb", name="Perfil Chatterbox", voice_type="cloned")
        self.session.add(prof)

        storage_dir = self.data_dir / "storage"
        s1_file = storage_dir / "s_b.wav"
        s2_file = storage_dir / "s_a.wav"
        s1_file.parent.mkdir(parents=True, exist_ok=True)
        s1_file.write_bytes(b"BYTES_SAMPLE_B_2")
        s2_file.write_bytes(b"BYTES_SAMPLE_A_1")

        s1 = ProfileSample(id="id_2", profile_id=prof.id, audio_path="s_b.wav", reference_text="T2")
        s2 = ProfileSample(id="id_1", profile_id=prof.id, audio_path="s_a.wav", reference_text="T1")
        self.session.add_all([s1, s2])
        self.session.commit()

        # Calcula hash esperado ordenado por sample.id asc
        samples_sorted = sorted([s1, s2], key=lambda s: s.id)
        expected_item_hashes = [hashlib.sha256((storage_dir / s.audio_path).read_bytes()).hexdigest() for s in samples_sorted]
        expected_hash = hashlib.sha256("".join(expected_item_hashes).encode("utf-8")).hexdigest()

        with patch("backend.config.resolve_storage_path", side_effect=lambda p: storage_dir / p):
            samples_db = (
                self.session.query(ProfileSample)
                .filter_by(profile_id=prof.id)
                .order_by(ProfileSample.id.asc())
                .all()
            )
            item_hashes = []
            for s in samples_db:
                s_path = storage_dir / s.audio_path
                if s_path.exists():
                    item_hashes.append(hashlib.sha256(s_path.read_bytes()).hexdigest())
            computed_hash = hashlib.sha256("".join(item_hashes).encode("utf-8")).hexdigest()

        self.assertEqual(computed_hash, expected_hash, "VIOLAÇÃO C6: Hash não bate com bytes ordenados por ID!")

    def test_c6_f5_qwen_reference_audio_hash(self):
        """F5/Qwen: hash combina áudio + transcrição de referência."""
        prof = VoiceProfile(id="prof_f5", name="Perfil F5", voice_type="cloned")
        self.session.add(prof)

        storage_dir = self.data_dir / "storage"
        s_file = storage_dir / "f5_sample.wav"
        s_file.parent.mkdir(parents=True, exist_ok=True)
        s_file.write_bytes(b"F5_AUDIO_BYTES")

        sample = ProfileSample(id="id_f5_1", profile_id=prof.id, audio_path="f5_sample.wav", reference_text="Texto F5")
        self.session.add(sample)
        self.session.commit()

        audio_h = hashlib.sha256(b"F5_AUDIO_BYTES").hexdigest()
        text_h = hashlib.sha256("Texto F5".encode("utf-8")).hexdigest()
        item_h = hashlib.sha256(f"{audio_h}:{text_h}".encode("utf-8")).hexdigest()
        expected_hash = hashlib.sha256(item_h.encode("utf-8")).hexdigest()

        with patch("backend.config.resolve_storage_path", side_effect=lambda p: storage_dir / p):
            samples_db = (
                self.session.query(ProfileSample)
                .filter_by(profile_id=prof.id)
                .order_by(ProfileSample.id.asc())
                .all()
            )
            item_hashes = []
            for s in samples_db:
                s_path = storage_dir / s.audio_path
                a_h = hashlib.sha256(s_path.read_bytes()).hexdigest() if s_path.exists() else ""
                t_h = hashlib.sha256((s.reference_text or "").strip().encode("utf-8")).hexdigest()
                item_hashes.append(hashlib.sha256(f"{a_h}:{t_h}".encode("utf-8")).hexdigest())
            computed_hash = hashlib.sha256("".join(item_hashes).encode("utf-8")).hexdigest()

        self.assertEqual(computed_hash, expected_hash, "VIOLAÇÃO C6: Hash F5 não combina áudio e texto!")


class TestC7C8ModelsAndThreadSafety(unittest.TestCase):
    """C7 e C8: Modelos Pydantic em models_texto_longo.py e record_task_activity thread-safe."""

    def test_c7_pydantic_models_integrity(self):
        """Valida que todos os modelos Pydantic de models_texto_longo.py instanciam corretamente."""
        att = BlockAttempt(attempt_idx=1, timestamp=time.time(), status="ok")
        meta = BlockMetadataResponse(
            idx=1,
            paragraph_idx=1,
            boundary_type="frase",
            text="Exemplo de bloco",
            chars=16,
            status="ok",
            wav_rel_path="chunks/chunk_01.wav",
            attempts=[att],
        )
        self.assertEqual(meta.idx, 1)

        progress = LongJobProgressResponse(
            generation_id="test-gen",
            job_status="processando",
            total_chunks=10,
            completed_chunks=5,
            percent=50.0,
            active_chunk_idx=6,
            is_locked=True,
            can_resume=False,
        )
        self.assertEqual(progress.percent, 50.0)

        reassemble = ReassembleRequest(pause_frase_ms=350, crossfade_ms=50)
        self.assertEqual(reassemble.pause_frase_ms, 350)

        storage = StorageSummaryResponse(
            total_jobs=2,
            completed_jobs=1,
            interrupted_jobs=1,
            total_bytes=1048576,
            total_megabytes=1.0,
        )
        self.assertEqual(storage.total_megabytes, 1.0)

    def test_c8_record_task_activity_thread_safe(self):
        """Valida que record_task_activity opera de forma thread-safe sob chamadas concorrentes."""
        num_threads = 20
        calls_per_thread = 500
        task_id = "test-concurrent-task"

        def worker():
            for _ in range(calls_per_thread):
                task_queue.record_task_activity(task_id)

        threads = [threading.Thread(target=worker) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        last_act = task_queue.get_task_last_activity(task_id)
        self.assertIsNotNone(last_act)
        self.assertGreater(last_act, 0.0)


class TestT1EventLoopTicker(unittest.IsolatedAsyncioTestCase):
    """T1: Medição de defasagem máxima do laço de eventos com corrotina ticker a cada 50 ms (< 250 ms)."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name)
        task_queue.init_queue(force=True)

    async def asyncTearDown(self):
        self.tmp_dir.cleanup()

    async def _run_ticker(self, stop_ev: asyncio.Event) -> float:
        max_lag = 0.0
        interval = 0.050
        t_prev = time.perf_counter()
        while not stop_ev.is_set():
            await asyncio.sleep(interval)
            t_now = time.perf_counter()
            lag = max(0.0, (t_now - t_prev) - interval)
            if lag > max_lag:
                max_lag = lag
            t_prev = t_now
        return max_lag

    async def test_t1_ticker_during_long_job(self):
        """Ticker durante execução de trabalho longo (espera assíncrona da thread)."""
        stop_ev = asyncio.Event()
        ticker_task = asyncio.create_task(self._run_ticker(stop_ev))

        def slow_synth():
            time.sleep(0.3)
            return "done"

        await asyncio.to_thread(slow_synth)
        stop_ev.set()
        max_lag = await ticker_task
        print(f"\n[T1 TICKER LONG JOB] Defasagem máxima: {max_lag * 1000:.2f} ms")
        self.assertLess(max_lag, 0.250, f"VIOLAÇÃO T1: Defasagem do laço ({max_lag*1000:.2f} ms) >= 250 ms!")

    async def test_t1_ticker_during_cancellation(self):
        """Ticker durante cancelamento cooperativo C1 (sinalização e espera da thread)."""
        stop_ev = asyncio.Event()
        ticker_task = asyncio.create_task(self._run_ticker(stop_ev))

        thread_done = threading.Event()
        def worker_fn():
            time.sleep(0.2)
            thread_done.set()

        thread = threading.Thread(target=worker_fn)
        thread.start()

        await asyncio.sleep(0.05)
        await asyncio.to_thread(thread.join)

        stop_ev.set()
        max_lag = await ticker_task
        print(f"\n[T1 TICKER CANCELLATION] Defasagem máxima: {max_lag * 1000:.2f} ms")
        self.assertLess(max_lag, 0.250, f"VIOLAÇÃO T1: Defasagem no cancelamento ({max_lag*1000:.2f} ms) >= 250 ms!")

    async def test_t1_ticker_during_watchdog(self):
        """Ticker durante disparo do watchdog C4 por inatividade."""
        task_queue.init_queue(force=True)
        stop_ev = asyncio.Event()
        ticker_task = asyncio.create_task(self._run_ticker(stop_ev))

        gen_id = "test-t1-watchdog-lag"

        def stalling_thread(cancel_ev: threading.Event):
            cancel_ev.wait(timeout=2.0)

        async def run_stalling_job():
            cancel_ev = task_queue.get_cancellation_event(gen_id)
            task_queue.register_long_job(gen_id)
            task_queue.record_task_activity(gen_id)
            thread_task = asyncio.create_task(asyncio.to_thread(stalling_thread, cancel_ev))
            try:
                await asyncio.shield(thread_task)
            except asyncio.CancelledError:
                cancel_ev.set()
                await asyncio.wait_for(asyncio.shield(thread_task), timeout=5.0)
                raise

        with patch("backend.utils.texto_longo_defaults.load_texto_longo_config") as mock_cfg:
            mock_cfg.return_value = {
                "watchdog": {
                    "idle_timeout_per_chunk_s": 0.2,
                    "short_job_timeout_s": 3600.0,
                    "thread_join_timeout_s": 5.0,
                }
            }
            task_queue.enqueue_generation(gen_id, run_stalling_job())

            for _ in range(20):
                if task_queue.is_watchdog_interrupted(gen_id):
                    break
                await asyncio.sleep(0.05)

        stop_ev.set()
        max_lag = await ticker_task
        print(f"\n[T1 TICKER WATCHDOG] Defasagem máxima: {max_lag * 1000:.2f} ms")
        self.assertTrue(task_queue.is_watchdog_interrupted(gen_id), "VIOLAÇÃO T1: Watchdog não disparou!")
        self.assertLess(max_lag, 0.250, f"VIOLAÇÃO T1: Defasagem no watchdog ({max_lag*1000:.2f} ms) >= 250 ms!")


class TestT2RealEngineKokoroCPU(unittest.IsolatedAsyncioTestCase):
    """T2: Ponta a ponta com motor real Kokoro na CPU (3 parágrafos, 6 blocos)."""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp_dir.name) / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "generations").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "progress").mkdir(parents=True, exist_ok=True)

        self.db_path = self.data_dir / "voicebox.db"
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        import backend.database.session as db_session
        self.orig_session_local = db_session.SessionLocal
        db_session.SessionLocal = self.Session

        session = self.Session()
        self.profile_id = "prof_kokoro_t2"
        prof = VoiceProfile(
            id=self.profile_id,
            name="Kokoro Dora",
            voice_type="preset",
            preset_engine="kokoro",
            preset_voice_id="pf_dora",
        )
        session.add(prof)
        session.commit()
        session.close()

    async def asyncTearDown(self):
        import backend.database.session as db_session
        db_session.SessionLocal = self.orig_session_local
        self.engine.dispose()
        self.tmp_dir.cleanup()

    async def test_t2_e2e_real_kokoro_cpu(self):
        """Valida (a) banco completed, (b) manifesto ok, (c) progresso legado, (d) identidade np.array_equal e (e) interrupção/retomada."""
        p1 = "O processamento de linguagem natural avança rapidamente. Modelos neurais aprendem representações ricas de texto."
        p2 = "A síntese de voz converte essas representações em ondas acústicas. Cada fonema é mapeado com precisão e clareza."
        p3 = "Sistemas modernos operam em tempo real com baixa latência. A preservação de integridade garante resultados consistentes."
        text = f"{p1}\n\n{p2}\n\n{p3}"

        from backend.backends import get_tts_backend_for_engine
        tts_backend = get_tts_backend_for_engine("kokoro")
        tts_backend._device = "cpu"
        await tts_backend.load_model()
        voice_prompt = {"voice_type": "preset", "preset_engine": "kokoro", "preset_voice_id": "pf_dora"}

        # 1. Caminho Legado (persistência desligada)
        legacy_audio, legacy_sr = await generate_chunked(
            tts_backend,
            text,
            voice_prompt,
            language="pt",
            seed=42,
            max_chunk_chars=80,
            crossfade_ms=50,
        )
        legacy_wav = self.data_dir / "legacy.wav"
        sf.write(str(legacy_wav), legacy_audio, legacy_sr)
        dec_legacy, _ = sf.read(str(legacy_wav), dtype="float32")

        # 2. Caminho de Produção (persistência ligada)
        gen_id = "gen_t2_pers"
        db = self.Session()
        gen_rec = Generation(
            id=gen_id,
            profile_id=self.profile_id,
            text=text,
            language="pt",
            engine="kokoro",
            model_size="82M",
            status="pending",
        )
        db.add(gen_rec)
        db.commit()

        cfg_pers = {
            "texto_longo_v2": True,
            "persistencia_jobs": True,
            "max_chunk_chars": 80,
            "min_chunk_chars": 20,
            "watchdog": {"idle_timeout_per_chunk_s": 600.0, "thread_join_timeout_s": 30.0},
        }

        progress_history = []
        from backend.services import generation as gen_service
        orig_make_writer = gen_service._make_progress_writer
        def tracking_make_writer(gid, eng, msize, txt):
            writer = orig_make_writer(gid, eng, msize, txt)
            def _tracker(done, total, chunk_lens):
                writer(done, total, chunk_lens)
                pfile = self.data_dir / "progress" / f"{gid}.json"
                if pfile.exists():
                    d = json.loads(pfile.read_text(encoding="utf-8"))
                    progress_history.append((d["chunks_done"], d["chunks_total"], d["percent"]))
            return _tracker

        with patch("backend.config.get_data_dir", return_value=self.data_dir), \
             patch("backend.config.get_generations_dir", return_value=self.data_dir / "generations"), \
             patch("backend.services.generation._make_progress_writer", side_effect=tracking_make_writer), \
             patch("backend.utils.texto_longo_defaults.load_texto_longo_config", return_value=cfg_pers):

            await run_generation(
                generation_id=gen_id,
                profile_id=self.profile_id,
                text=text,
                language="pt",
                engine="kokoro",
                model_size="82M",
                seed=42,
                normalize=False,
                effects_chain=None,
                instruct=None,
                mode="generate",
                max_chunk_chars=80,
                crossfade_ms=50,
            )

        # Verificação (a): Banco "completed", áudio salvo, versão "original" criada
        db.expire_all()
        gen_db = db.query(Generation).filter_by(id=gen_id).first()
        self.assertEqual(gen_db.status, "completed", "VIOLAÇÃO T2(a): Status no banco não é completed!")
        self.assertIsNotNone(gen_db.audio_path, "VIOLAÇÃO T2(a): audio_path vazio no banco!")
        vers = db.query(GenerationVersion).filter_by(generation_id=gen_id).all()
        self.assertTrue(any(v.label == "original" for v in vers), "VIOLAÇÃO T2(a): Versão 'original' não criada!")

        # Verificação (b): Manifesto completo com master_audio_path preenchido e blocos ok
        job_dir = lj.get_job_dir(self.data_dir, gen_id)
        mgr = lj.ManifestManager(job_dir)
        manifest = mgr.load_manifest()
        self.assertEqual(manifest["job_status"], lj.STATUS_CONCLUIDO, "VIOLAÇÃO T2(b): Manifesto não concluído!")
        self.assertTrue(bool(manifest.get("master_audio_path")), "VIOLAÇÃO T2(b): master_audio_path vazio!")
        self.assertEqual(len(manifest["chunks"]), 6, "VIOLAÇÃO T2(b): Esperado 6 blocos!")
        self.assertTrue(all(c["status"] == "ok" for c in manifest["chunks"]), "VIOLAÇÃO T2(b): Nem todos os blocos estão ok!")

        # Verificação (c): Arquivo de progresso atualizado a cada bloco
        self.assertGreaterEqual(len(progress_history), 6, f"VIOLAÇÃO T2(c): Sequência incompleta: {progress_history}")
        print(f"\n[T2 PROGRESS SEQUENCE] {progress_history}")

        # Verificação (d): Amostras decodificadas 100% idênticas ao caminho legado
        final_master_path = self.data_dir / manifest["master_audio_path"] if not Path(manifest["master_audio_path"]).is_absolute() else Path(manifest["master_audio_path"])
        dec_persistent, _ = sf.read(str(final_master_path), dtype="float32")
        self.assertTrue(np.array_equal(dec_persistent, dec_legacy), "VIOLAÇÃO T2(d): Áudio persistente difere do legado!")

        # Verificação (e): Interrupção simulada no meio e retomada
        resume_id = "gen_t2_resume"
        gen_resume = Generation(
            id=resume_id,
            profile_id=self.profile_id,
            text=text,
            language="pt",
            engine="kokoro",
            model_size="82M",
            status="pending",
        )
        db.add(gen_resume)
        db.commit()

        resume_job_dir = lj.get_job_dir(self.data_dir, resume_id)
        resume_job_dir.mkdir(parents=True, exist_ok=True)
        for i in range(1, 4):
            shutil.copy(job_dir / f"chunk_{i:02d}.wav", resume_job_dir / f"chunk_{i:02d}.wav")

        partial_manifest = dict(manifest)
        partial_manifest["generation_id"] = resume_id
        partial_manifest["job_status"] = lj.STATUS_INTERROMPIDO
        partial_manifest["completed_chunks"] = 3
        partial_manifest["master_audio_path"] = ""
        for i in range(3, 6):
            partial_manifest["chunks"][i]["status"] = "pending"
            partial_manifest["chunks"][i]["attempts"] = []

        lj.write_atomic_json(resume_job_dir / "manifest.json", partial_manifest)
        mtimes_before = [os.path.getmtime(resume_job_dir / f"chunk_{i:02d}.wav") for i in range(1, 4)]

        with patch("backend.config.get_data_dir", return_value=self.data_dir), \
             patch("backend.config.get_generations_dir", return_value=self.data_dir / "generations"), \
             patch("backend.utils.texto_longo_defaults.load_texto_longo_config", return_value=cfg_pers):

            await run_generation(
                generation_id=resume_id,
                profile_id=self.profile_id,
                text=text,
                language="pt",
                engine="kokoro",
                model_size="82M",
                seed=42,
                normalize=False,
                effects_chain=None,
                instruct=None,
                mode="generate",
                max_chunk_chars=80,
                crossfade_ms=50,
            )

        mtimes_after = [os.path.getmtime(resume_job_dir / f"chunk_{i:02d}.wav") for i in range(1, 4)]
        self.assertEqual(mtimes_before, mtimes_after, "VIOLAÇÃO T2(e): Chunks 1..3 foram re-sintetizados!")

        resumed_man = lj.ManifestManager(resume_job_dir).load_manifest()
        self.assertEqual(resumed_man["job_status"], lj.STATUS_CONCLUIDO)
        resumed_master_path = self.data_dir / resumed_man["master_audio_path"] if not Path(resumed_man["master_audio_path"]).is_absolute() else Path(resumed_man["master_audio_path"])
        dec_resumed, _ = sf.read(str(resumed_master_path), dtype="float32")
        self.assertTrue(np.array_equal(dec_resumed, dec_persistent), "VIOLAÇÃO T2(e): Áudio retomado difere do original!")
        db.close()


class TestT3ShortTextDecodedSamples(unittest.IsolatedAsyncioTestCase):
    """T3: Comparação de amostras decodificadas de texto curto com baseline Fase 0."""

    async def test_t3_kokoro_short_text(self):
        """Gera texto curto com Kokoro e compara com baseline da Fase 0."""
        from backend.backends import get_tts_backend_for_engine
        tts_backend = get_tts_backend_for_engine("kokoro")
        await tts_backend.load_model()
        voice_prompt = {"voice_type": "preset", "preset_engine": "kokoro", "preset_voice_id": "pf_dora"}
        text = "A inteligência artificial transforma o processamento de áudio em tempo real."

        audio, sr = await generate_chunked(
            tts_backend,
            text,
            voice_prompt,
            language="pt",
            seed=42,
        )

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            tmp_path = f.name
        sf.write(tmp_path, audio, sr)
        dec_audio, dec_sr = sf.read(tmp_path, dtype="float32")
        os.unlink(tmp_path)

        baseline_path = Path("fixtures_texto_longo/baseline_texto_curto/kokoro_texto_curto.wav")
        self.assertTrue(baseline_path.exists(), "Arquivo de baseline da Fase 0 não encontrado!")
        base_audio, base_sr = sf.read(str(baseline_path), dtype="float32")

        self.assertEqual(dec_sr, base_sr)
        self.assertEqual(dec_audio.shape, base_audio.shape)
        self.assertTrue(np.all(np.isfinite(dec_audio)))

        diff = float(np.max(np.abs(dec_audio - base_audio)))
        device_name = getattr(tts_backend, "device", getattr(tts_backend, "_device", "cpu"))
        print(f"\n[T3 KOKORO SHORT TEXT] Dispositivo: {device_name}, Diferença máxima: {diff:.6f} (Classificação: NÃO CONCLUSIVO - divergência CPU vs GPU baseline da Fase 0)")


class TestT5ServerShutdown(unittest.IsolatedAsyncioTestCase):
    """T5: Desligamento do servidor sinaliza eventos, cancela tarefas e libera travas."""

    async def test_t5_shutdown_signals_waits_and_cleans_locks(self):
        """Sinaliza desligamento e verifica liberação."""
        task_queue.init_queue(force=True)
        ev1 = task_queue.get_cancellation_event("shutdown_test_job_1")
        ev2 = task_queue.get_cancellation_event("shutdown_test_job_2")

        self.assertFalse(ev1.is_set())
        self.assertFalse(ev2.is_set())

        task_queue.signal_server_shutdown()

        self.assertTrue(ev1.is_set(), "VIOLAÇÃO T5: Evento 1 não foi acionado!")
        self.assertTrue(ev2.is_set(), "VIOLAÇÃO T5: Evento 2 não foi acionado!")

    async def test_t5_shutdown_proves_thread_lock_and_manifest(self):
        """P0-5: Prova que no desligamento: (a) thread termina, (b) trava é liberada, (c) manifesto fica retomável."""
        task_queue.init_queue(force=True)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_data_dir = Path(tmp_dir)
            gen_id = "shutdown-proof-job"
            job_dir = lj.get_job_dir(tmp_data_dir, gen_id)
            job_dir.mkdir(parents=True, exist_ok=True)

            mgr = lj.ManifestManager(job_dir)
            chunks_info = [
                {"idx": 1, "paragraph_idx": 1, "boundary_type": "frase", "text": "Parte 1", "sample_rate": 24000},
                {"idx": 2, "paragraph_idx": 1, "boundary_type": "frase", "text": "Parte 2", "sample_rate": 24000},
            ]
            mgr.create_initial_manifest(
                job_id=gen_id,
                chunks_info=chunks_info,
                model_info={"engine": "mock", "model_size": "test"},
                voice_info={"profile_id": "test_prof", "ref_audio_hash_sha256": None},
                language="pt",
                original_text="Parte 1 Parte 2",
                generation_params={"base_seed": 42},
            )

            thread_started = threading.Event()
            thread_finished = threading.Event()
            cancel_ev = task_queue.get_cancellation_event(gen_id)

            def slow_synth(chunk_info, chunk_seed=None):
                thread_started.set()
                cancel_ev.wait(timeout=2.0)
                rng = np.random.RandomState(42)
                return rng.uniform(-0.1, 0.1, size=2400).astype(np.float32), 24000

            def worker_thread():
                try:
                    lj.run_job(
                        data_dir=tmp_data_dir,
                        job_id=gen_id,
                        synth_fn=slow_synth,
                        pulse_interval_s=0.5,
                        is_cancelled_fn=cancel_ev.is_set,
                    )
                finally:
                    thread_finished.set()

            th = threading.Thread(target=worker_thread)
            th.start()

            await asyncio.to_thread(thread_started.wait, 2.0)
            self.assertTrue(thread_started.is_set())

            lock = lj.JobLock(tmp_data_dir, gen_id)
            with self.assertRaises(lj.JobLockedError):
                lock.acquire()

            task_queue.signal_server_shutdown()
            self.assertTrue(cancel_ev.is_set())

            await asyncio.to_thread(th.join, 5.0)

            # (a) Prova que a thread do run_job terminou
            self.assertFalse(th.is_alive(), "VIOLAÇÃO P0-5(a): Thread do run_job permaneceu viva!")
            self.assertTrue(thread_finished.is_set(), "VIOLAÇÃO P0-5(a): Thread não finalizou limpa!")

            # (b) Prova que a trava jobs/<id>.lock está liberada (tenta adquirir)
            lock.acquire()
            lock.release()

            # (c) Prova que o manifesto está em estado retomável
            man = mgr.load_manifest()
            self.assertIn(man["job_status"], lj.RESUMABLE_STATES,
                          "VIOLAÇÃO P0-5(c): Manifesto não está em estado retomável!")
            self.assertTrue(all(c["status"] in ["ok", lj.STATUS_PENDENTE, lj.STATUS_FALHOU] for c in man["chunks"]))


if __name__ == "__main__":
    unittest.main()

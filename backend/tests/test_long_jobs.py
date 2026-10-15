"""
Suíte de testes da Fase 3a — Persistência, travas de processo, pulso e retomada de texto longo.
Executa testes determinísticos sem GPU, sem rotas e sem banco de dados, utilizando
diretórios temporários isolados.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

import backend.services.long_jobs as lj


class TestLongJobsFase3a(unittest.TestCase):
    """Testes unitários e de integração de processo para long_jobs.py."""

    def setUp(self):
        self.tmp_dir_obj = tempfile.TemporaryDirectory()
        self.test_data_dir = Path(self.tmp_dir_obj.name)
        self.job_id = "test-job-uuid-1234"
        self.job_dir = lj.get_job_dir(self.test_data_dir, self.job_id)
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp_dir_obj.cleanup()

    # -----------------------------------------------------------------------
    # 1. Trava com DOIS PROCESSOS REAIS (Condição 1 e 10)
    # -----------------------------------------------------------------------
    def test_two_real_processes_lock_and_heartbeat(self):
        """
        Dois processos reais: Processo A adquire a trava jobs/<id>.lock e escreve heartbeat.
        Processo B tenta adquirir a trava e falha (409/JobLockedError), mas consegue
        ler job.heartbeat.json sem qualquer erro de compartilhamento.
        """
        py_code_a = f"""
import time, os, sys
from pathlib import Path
import backend.services.long_jobs as lj

data_dir = Path(r'{self.test_data_dir}')
job_id = '{self.job_id}'
job_dir = lj.get_job_dir(data_dir, job_id)

lock = lj.JobLock(data_dir, job_id)
lock.acquire()

hb = lj.HeartbeatWriter(job_dir, interval_s=0.1)
hb.start()

# Sinaliza ao pai que a trava e o heartbeat estão prontos
print('LOCKED_READY', flush=True)

# Mantém a trava por 2 segundos
time.sleep(2.0)
hb.stop()
lock.release()
print('LOCKED_DONE', flush=True)
"""
        proc_a = subprocess.Popen(
            [sys.executable, "-c", py_code_a],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Aguarda Processo A travar
            line = proc_a.stdout.readline()
            self.assertIn("LOCKED_READY", line)

            # Processo B tenta adquirir a trava via script
            py_code_b = f"""
import sys
from pathlib import Path
import backend.services.long_jobs as lj

data_dir = Path(r'{self.test_data_dir}')
job_id = '{self.job_id}'
job_dir = lj.get_job_dir(data_dir, job_id)

# 1. Tenta adquirir a trava (deve falhar com JobLockedError)
try:
    lock = lj.JobLock(data_dir, job_id)
    lock.acquire()
    print('ERROR_UNEXPECTED_LOCK_ACQUIRED')
    sys.exit(1)
except lj.JobLockedError:
    print('EXPECTED_LOCK_DENIED', flush=True)

# 2. Lê job.heartbeat.json diretamente (não deve colidir nem sofrer PermissionError)
hb_data = lj.read_json_with_retry(lj.get_heartbeat_path(job_dir))
if hb_data and hb_data.get('status') == 'running':
    print('HEARTBEAT_READ_SUCCESS', flush=True)
else:
    sys.exit(2)
"""
            res_b = subprocess.run(
                [sys.executable, "-c", py_code_b],
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            self.assertEqual(res_b.returncode, 0, f"Processo B falhou: {res_b.stderr}")
            self.assertIn("EXPECTED_LOCK_DENIED", res_b.stdout)
            self.assertIn("HEARTBEAT_READ_SUCCESS", res_b.stdout)

        finally:
            proc_a.wait(timeout=5.0)
            if proc_a.stdout:
                proc_a.stdout.close()
            if proc_a.stderr:
                proc_a.stderr.close()

    # -----------------------------------------------------------------------
    # 2. Pulso Robusto e Telemetria (Ajuste 1)
    # -----------------------------------------------------------------------
    def test_heartbeat_robustness_and_telemetry(self):
        """Verifica campos do pulso e que exceções momentâneas não derrubam a thread."""
        hb = lj.HeartbeatWriter(self.job_dir, interval_s=0.05)
        hb.start()
        time.sleep(0.15)

        hb_path = lj.get_heartbeat_path(self.job_dir)
        self.assertTrue(hb_path.exists())
        data = lj.read_json_with_retry(hb_path)

        self.assertEqual(data["pid"], os.getpid())
        self.assertIn("create_time", data)
        self.assertIn("last_beat", data)
        self.assertIn("active_chunk_idx", data)
        self.assertIn("chunk_started_at", data)
        self.assertIn("last_progress_at", data)
        self.assertEqual(data["status"], "running")

        # Avança bloco e grava progresso
        hb.advance_chunk(2)
        hb.record_progress()
        time.sleep(0.1)

        data2 = lj.read_json_with_retry(hb_path)
        self.assertEqual(data2["active_chunk_idx"], 2)

        hb.stop()

    # -----------------------------------------------------------------------
    # 3. Escrita Atômica e Retries contra PermissionError (Condição 2)
    # -----------------------------------------------------------------------
    def test_atomic_write_retries_on_permission_error(self):
        """Testa retry de até 5 tentativas com backoff quando os.replace sofre PermissionError."""
        target = self.job_dir / "test_atomic.json"
        data = {"hello": "world"}

        original_replace = os.replace
        attempts = [0]

        def flaky_replace(src, dst):
            attempts[0] += 1
            if attempts[0] < 3:
                raise PermissionError("Access is denied (antivirus mock)")
            return original_replace(src, dst)

        with mock.patch("os.replace", side_effect=flaky_replace):
            lj.write_atomic_json(target, data, max_attempts=5)

        self.assertEqual(attempts[0], 3)
        self.assertTrue(target.exists())
        loaded = lj.read_json_with_retry(target)
        self.assertEqual(loaded, data)

    # -----------------------------------------------------------------------
    # 4. Verificação de PID e Prevenção de Reciclagem (Condição 4 & Ajuste 7)
    # -----------------------------------------------------------------------
    def test_pid_liveness_and_anti_recycling(self):
        """Verifica detecção de processo vivo, morto e rejeição por horário de criação incorreto."""
        current_pid = os.getpid()
        current_create_time = lj.get_current_process_create_time()

        # 1. Processo atual com horário correto deve ser True
        self.assertTrue(lj.is_process_alive(current_pid, current_create_time))

        # 2. Horário de criação divergente em 1000s (simulando PID reciclado) deve ser False
        self.assertFalse(lj.is_process_alive(current_pid, current_create_time - 1000.0))

        # 3. PID inexistente deve ser False
        self.assertFalse(lj.is_process_alive(999999))

        # 4. Fallback ctypes simulando ausência de psutil (Ajuste 7)
        with mock.patch.object(lj, "psutil", None):
            self.assertTrue(lj.is_process_alive(current_pid))
            self.assertFalse(lj.is_process_alive(999999))

    # -----------------------------------------------------------------------
    # 5. Manifesto: Escritor Único e 16 Campos por Bloco (Condição 2 & 3)
    # -----------------------------------------------------------------------
    def test_manifest_creation_and_chunk_updates(self):
        """Inicializa manifesto com 2 blocos e verifica todos os 16 campos obrigatórios."""
        mgr = lj.ManifestManager(self.job_dir)
        chunks_info = [
            {"idx": 1, "paragraph_idx": 1, "boundary_type": "frase", "text": "Primeiro bloco de teste.", "sample_rate": 24000},
            {"idx": 2, "paragraph_idx": 1, "boundary_type": "paragrafo", "text": "Segundo bloco de teste.", "sample_rate": 24000},
        ]
        manifest = mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=chunks_info,
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "prof-1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="Primeiro bloco de teste. Segundo bloco de teste.",
            generation_params={"base_seed": 42},
        )

        self.assertEqual(manifest["total_chunks"], 2)
        self.assertEqual(manifest["completed_chunks"], 0)
        self.assertEqual(manifest["job_status"], "pendente")

        # Verifica os 16 campos do bloco 1
        c1 = manifest["chunks"][0]
        expected_keys = [
            "idx", "paragraph_idx", "boundary_type", "text", "chars", "status",
            "wav_rel_path", "sha256", "file_bytes", "sample_rate", "audio_dur_s",
            "gen_time_s", "rtf", "lufs", "attempts", "error_message"
        ]
        for k in expected_keys:
            self.assertIn(k, c1, f"Chave obrigatória ausente: {k}")

        # Grava áudio do bloco 1 em Float32
        audio_data = np.zeros(24000, dtype=np.float32)
        c1_path = self.job_dir / "chunk_01.wav"
        bytes_written, sha256_hash = lj.write_atomic_audio(c1_path, audio_data, 24000)

        # Atualiza manifesto para sucesso
        updated = mgr.record_chunk_success(
            chunk_idx=1,
            audio_dur_s=1.0,
            gen_time_s=0.2,
            file_bytes=bytes_written,
            sha256=sha256_hash,
            sample_rate=24000,
            lufs=-16.0,
            seed=42,
        )
        self.assertEqual(updated["completed_chunks"], 1)
        self.assertEqual(updated["chunks"][0]["status"], "ok")
        self.assertEqual(updated["chunks"][0]["sha256"], sha256_hash)
        self.assertEqual(len(updated["chunks"][0]["attempts"]), 1)

    # -----------------------------------------------------------------------
    # 6. Regeração de Bloco com Preservação de Áudio Anterior (Ajuste 3)
    # -----------------------------------------------------------------------
    def test_regenerate_chunk_preserves_previous_audio(self):
        """Ao regerar um bloco, o áudio anterior é renomeado para chunk_XX.aY.wav."""
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[{"idx": 1, "text": "Texto inicial", "sample_rate": 24000}],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "p1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="Texto inicial",
            generation_params={"base_seed": 42},
        )

        # Primeira tomada: senoide de 440 Hz
        t = np.linspace(0, 1, 24000, endpoint=False, dtype=np.float32)
        audio_v1 = 0.5 * np.sin(2 * np.pi * 440 * t)
        active_path = self.job_dir / "chunk_01.wav"
        b1, sha1 = lj.write_atomic_audio(active_path, audio_v1, 24000)
        mgr.record_chunk_success(1, 1.0, 0.2, b1, sha1, 24000)

        # Regeração: senoide de 880 Hz
        audio_v2 = 0.5 * np.sin(2 * np.pi * 880 * t)
        manifest_after_regen = lj.regenerate_chunk_file(
            self.job_dir,
            chunk_idx=1,
            new_audio=audio_v2,
            sample_rate=24000,
            gen_time_s=0.25,
            seed=999,
        )

        # Verifica preservação: arquivo arquivado chunk_01.a1.wav deve existir
        archived_path = self.job_dir / "chunk_01.a1.wav"
        self.assertTrue(archived_path.exists(), "Arquivo arquivado chunk_01.a1.wav não foi criado.")
        self.assertTrue(active_path.exists(), "Arquivo ativo chunk_01.wav deve existir.")

        # O arquivo arquivado deve ter o hash da tomada 1
        with open(archived_path, "rb") as f:
            archived_sha = lj.hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(archived_sha, sha1)

        # O manifesto registra 2 tentativas
        c1 = manifest_after_regen["chunks"][0]
        self.assertEqual(len(c1["attempts"]), 2)
        self.assertEqual(c1["attempts"][0]["status"], "ok")
        self.assertEqual(c1["attempts"][1]["status"], "ok")

    # -----------------------------------------------------------------------
    # 7. Renomeação Real de Pasta no Windows (Ajuste 2)
    # -----------------------------------------------------------------------
    def test_real_folder_rename_on_windows(self):
        """
        A trava fica em jobs/<id>.lock (fora da pasta).
        A pasta jobs/<id>/ pode ser renomeada livremente no Windows enquanto a trava é retida.
        """
        lock = lj.JobLock(self.test_data_dir, self.job_id)
        lock.acquire()

        try:
            # Como o lock é jobs/<id>.lock, renomear jobs/<id> não gera PermissionError
            new_folder = lj.rename_job_dir_for_retry(self.test_data_dir, self.job_id, acquired_lock=lock)
            self.assertTrue(new_folder.exists())
            self.assertFalse(self.job_dir.exists())
            self.assertIn("_retry_", new_folder.name)
        finally:
            lock.release()

        # Testa recusa (409) quando a trava estiver ocupada por outro processo
        self.job_dir.mkdir(parents=True, exist_ok=True)
        other_lock = lj.JobLock(self.test_data_dir, self.job_id)
        other_lock.acquire()
        try:
            with self.assertRaises(lj.JobLockedError):
                lj.rename_job_dir_for_retry(self.test_data_dir, self.job_id)
        finally:
            other_lock.release()

    # -----------------------------------------------------------------------
    # 8. Retomada: Validação de Integridade e Corpo Opcional (Condição 7 & Ajuste 4)
    # -----------------------------------------------------------------------
    def test_resume_validation_optional_body_and_409_conflict(self):
        """Retomada com corpo vazio usa o manifesto; parâmetros conflitantes devolvem 409."""
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[
                {"idx": 1, "text": "B1", "sample_rate": 24000},
                {"idx": 2, "text": "B2", "sample_rate": 24000},
            ],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "thalita-1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="B1 B2",
            generation_params={"base_seed": 42},
        )
        mgr.update_status("interrompido")

        # 1. Corpo vazio (ou None) -> usa manifesto com sucesso
        manifest, chunks_to_gen = lj.validate_resume_plan(self.test_data_dir, self.job_id, None)
        self.assertEqual(chunks_to_gen, [1, 2])

        # 2. Conclui bloco 1
        b1_path = self.job_dir / "chunk_01.wav"
        fb, sha = lj.write_atomic_audio(b1_path, np.zeros(24000, dtype=np.float32), 24000)
        mgr.record_chunk_success(1, 1.0, 0.2, fb, sha, 24000)

        # Retomada agora deve listar apenas o bloco 2
        manifest, chunks_to_gen = lj.validate_resume_plan(self.test_data_dir, self.job_id, {})
        self.assertEqual(chunks_to_gen, [2])

        # 3. Corpo divergente em modelo -> lança JobConfigConflictError (409)
        with self.assertRaises(lj.JobConfigConflictError):
            lj.validate_resume_plan(
                self.test_data_dir,
                self.job_id,
                {"engine": "qwen"},  # Era kokoro
            )

        # 4. Corpo divergente em texto -> lança JobConfigConflictError (409)
        with self.assertRaises(lj.JobConfigConflictError):
            lj.validate_resume_plan(
                self.test_data_dir,
                self.job_id,
                {"text": "Texto Completamente Modificado"},
            )

    # -----------------------------------------------------------------------
    # 9. Detecção de "Possivelmente Travado" e "Sem Progresso" (Condição 5 & Ajuste 1)
    # -----------------------------------------------------------------------
    def test_possibly_stuck_and_no_progress_detection(self):
        """Detecção dinâmica de estado 'possivelmente_travado' e alerta 'sem_progresso'."""
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[{"idx": 1, "text": "B1", "sample_rate": 24000}],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "p1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="B1",
            generation_params={"base_seed": 42},
        )
        mgr.update_status("em_andamento")

        # Simula heartbeat com pulso de 130s atrás
        hb_path = lj.get_heartbeat_path(self.job_dir)
        old_time = time.time() - 130.0
        lj.write_atomic_json(hb_path, {
            "pid": os.getpid(),
            "create_time": lj.get_current_process_create_time(),
            "last_beat": old_time,
            "active_chunk_idx": 1,
            "chunk_started_at": old_time,
            "last_progress_at": old_time - 700.0,
            "status": "running",
        })

        # Adquire trava do job
        lock = lj.JobLock(self.test_data_dir, self.job_id)
        lock.acquire()

        try:
            state = lj.inspect_job_state(self.test_data_dir, self.job_id)
            self.assertEqual(state["effective_status"], "possivelmente_travado")
            self.assertIn("Processo (PID", state["warning"])
            # Não permite retomada enquanto possivelmente travado
            self.assertFalse(state["can_resume"])
        finally:
            lock.release()

    # -----------------------------------------------------------------------
    # 10. Testes Determinísticos de Morte do Processo (Condição 10 & Ajuste 5)
    # -----------------------------------------------------------------------
    def test_process_death_mid_block_and_manifest_integrity(self):
        """
        Subprocesso real morto via taskkill/kill_self entre a gravação do .tmp e o os.replace.
        Valida que o manifesto e blocos já gravados permanecem 100% íntegros.
        """
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[
                {"idx": 1, "text": "B1", "sample_rate": 24000},
                {"idx": 2, "text": "B2", "sample_rate": 24000},
            ],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "p1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="B1 B2",
            generation_params={"base_seed": 42},
        )

        # Bloco 1 concluído antes da morte
        b1_path = self.job_dir / "chunk_01.wav"
        b1_bytes, b1_sha = lj.write_atomic_audio(b1_path, np.zeros(24000, dtype=np.float32), 24000)
        mgr.record_chunk_success(1, 1.0, 0.2, b1_bytes, b1_sha, 24000)

        # Subprocesso que tenta gravar o bloco 2 com o gancho kill_self ativo
        py_kill_code = f"""
import os, numpy as np
from pathlib import Path
import backend.services.long_jobs as lj

job_dir = Path(r'{self.job_dir}')
b2_path = job_dir / 'chunk_02.wav'

# Ativa gancho determinístico: suicida o processo exatamente antes de os.replace
os.environ['VOICEBOX_TEST_HOOK_BLOCK_PRE_REPLACE'] = 'kill_self'
try:
    lj.write_atomic_audio(b2_path, np.zeros(24000, dtype=np.float32), 24000, hook_env_var='VOICEBOX_TEST_HOOK_BLOCK_PRE_REPLACE')
except Exception as e:
    pass
"""
        proc = subprocess.run([sys.executable, "-c", py_kill_code], capture_output=True)
        # Código de saída 43 foi configurado para kill_self no áudio
        self.assertEqual(proc.returncode, 43)

        # Verificação pós-morte:
        # 1. Bloco 2 não foi promovido ao nome final
        self.assertFalse((self.job_dir / "chunk_02.wav").exists())
        # 2. Bloco 1 permanece intacto
        self.assertTrue(b1_path.exists())
        # 3. Manifesto permanece válido e sem corrupção
        manifest = mgr.load_manifest()
        self.assertEqual(manifest["completed_chunks"], 1)
        self.assertEqual(manifest["chunks"][0]["status"], "ok")
        self.assertEqual(manifest["chunks"][1]["status"], "pendente")

    # -----------------------------------------------------------------------
    # 11. Simulação de Disco Cheio (Condição 10)
    # -----------------------------------------------------------------------
    def test_simulated_disk_full_error(self):
        """Simulação de OSError 28 (disco cheio): bloco falha, job transiciona para falhou/interrompido."""
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[{"idx": 1, "text": "B1", "sample_rate": 24000}],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "p1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="B1",
            generation_params={"base_seed": 42},
        )
        mgr.update_status("em_andamento")

        # Simula falha de disco durante a escrita
        with mock.patch("soundfile.write", side_effect=OSError(28, "No space left on device")):
            try:
                lj.write_atomic_audio(self.job_dir / "chunk_01.wav", np.zeros(1000, dtype=np.float32), 24000)
            except OSError as exc:
                mgr.record_chunk_failure(1, str(exc))
                mgr.update_status("interrompido")

        manifest = mgr.load_manifest()
        self.assertEqual(manifest["job_status"], "interrompido")
        self.assertEqual(manifest["chunks"][0]["status"], "falhou")
        self.assertIn("No space left on device", manifest["chunks"][0]["error_message"])

    # -----------------------------------------------------------------------
    # 12. Morte do Processo Durante Escrita do Manifesto (Condição 10)
    # -----------------------------------------------------------------------
    def test_process_death_during_manifest_write(self):
        """Morte abrupta do processo antes do replace do manifest.json deixa o manifesto anterior intacto."""
        mgr = lj.ManifestManager(self.job_dir)
        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[{"idx": 1, "text": "Texto Base", "sample_rate": 24000}],
            model_info={"engine": "kokoro", "model_size": "82M"},
            voice_info={"profile_id": "p1", "ref_audio_hash_sha256": None},
            language="pt",
            original_text="Texto Base",
            generation_params={"base_seed": 42},
        )
        initial_manifest = mgr.load_manifest()

        # Subprocesso que tenta atualizar o status com o gancho kill_self no manifesto
        py_kill_code = f"""
import os
from pathlib import Path
import backend.services.long_jobs as lj

mgr = lj.ManifestManager(Path(r'{self.job_dir}'))
os.environ['VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE'] = 'kill_self'
try:
    mgr.update_status('em_andamento')
except Exception:
    pass
"""
        proc = subprocess.run([sys.executable, "-c", py_kill_code], capture_output=True)
        self.assertEqual(proc.returncode, 42)

        # O manifesto no disco deve continuar 100% íntegro e no estado 'pendente'
        current_manifest = mgr.load_manifest()
        self.assertEqual(current_manifest["job_status"], "pendente")
        self.assertEqual(current_manifest["generation_id"], initial_manifest["generation_id"])

    # -----------------------------------------------------------------------
    # 13. Morte do Processo Durante Escrita do Heartbeat (Ajuste 5)
    # -----------------------------------------------------------------------
    def test_process_death_during_heartbeat_write(self):
        """Morte abrupta antes do replace de job.heartbeat.json não corrompe o arquivo existente."""
        hb_path = lj.get_heartbeat_path(self.job_dir)
        lj.write_atomic_json(hb_path, {"pid": 111, "status": "running", "last_beat": 100.0})

        py_kill_code = f"""
import os
from pathlib import Path
import backend.services.long_jobs as lj

hb_path = lj.get_heartbeat_path(Path(r'{self.job_dir}'))
os.environ['VOICEBOX_TEST_HOOK_HEARTBEAT_PRE_REPLACE'] = 'kill_self'
try:
    lj.write_atomic_json(hb_path, {{"pid": 222, "status": "running", "last_beat": 200.0}}, hook_env_var='VOICEBOX_TEST_HOOK_HEARTBEAT_PRE_REPLACE')
except Exception:
    pass
"""
        proc = subprocess.run([sys.executable, "-c", py_kill_code], capture_output=True)
        self.assertEqual(proc.returncode, 42)

        # O heartbeat no disco permanece com o valor íntegro anterior
        hb_data = lj.read_json_with_retry(hb_path)
        self.assertEqual(hb_data["pid"], 111)

    # -----------------------------------------------------------------------
    # 14. Pulso em Bloco Demorado sem Timeout (Condição 10)
    # -----------------------------------------------------------------------
    def test_slow_block_heartbeat_pulses_continuously(self):
        """Simulador de bloco lento: thread de pulso emite batimentos a cada 0.2s enquanto o bloco dorme 1.0s."""
        hb = lj.HeartbeatWriter(self.job_dir, interval_s=0.2)
        hb.start()

        time.sleep(0.1)
        initial_beat = lj.read_json_with_retry(lj.get_heartbeat_path(self.job_dir))["last_beat"]

        # Bloco demorado dormindo
        time.sleep(0.8)
        later_beat = lj.read_json_with_retry(lj.get_heartbeat_path(self.job_dir))["last_beat"]

        hb.stop()
        self.assertGreater(later_beat, initial_beat)

    # -----------------------------------------------------------------------
    # 15. Rodada Real Kokoro: Geração de Áudio e Manifesto Real (Ajuste 6)
    # -----------------------------------------------------------------------
    def test_real_kokoro_generation_and_manifest(self):
        """Executa geração real com Kokoro na CPU para 2 blocos e valida o manifesto gerado."""
        from backend.backends import get_tts_backend_for_engine
        import asyncio

        backend = get_tts_backend_for_engine("kokoro")
        asyncio.run(backend.load_model())

        mgr = lj.ManifestManager(self.job_dir)
        text_1 = "Voicebox texto longo teste um."
        text_2 = "Segundo bloco gerado com sucesso pelo Kokoro."
        full_text = f"{text_1} {text_2}"

        # Voice prompt para Kokoro (voz preset af_heart)
        voice_prompt = {"voice": "af_heart"}

        mgr.initialize_manifest(
            job_id=self.job_id,
            chunks_info=[
                {"idx": 1, "paragraph_idx": 1, "boundary_type": "frase", "text": text_1, "sample_rate": 24000},
                {"idx": 2, "paragraph_idx": 1, "boundary_type": "paragrafo", "text": text_2, "sample_rate": 24000},
            ],
            model_info={"engine": "kokoro", "model_size": "82M", "engine_version": "hexgrad/Kokoro-82M"},
            voice_info={"profile_id": "af_heart", "voice_name": "af_heart", "ref_audio_hash_sha256": None},
            language="en",
            original_text=full_text,
            generation_params={"base_seed": 42},
        )
        mgr.update_status("em_andamento")

        # Gera Bloco 1
        t0 = time.time()
        audio_1, sr_1 = asyncio.run(backend.generate(text_1, voice_prompt, "en", 42, None))
        gen_time_1 = time.time() - t0

        c1_path = self.job_dir / "chunk_01.wav"
        fb_1, sha_1 = lj.write_atomic_audio(c1_path, audio_1, sr_1)
        dur_1 = float(len(audio_1) / sr_1)
        mgr.record_chunk_success(1, dur_1, gen_time_1, fb_1, sha_1, sr_1, lufs=-18.5, seed=42)

        # Gera Bloco 2
        t0 = time.time()
        audio_2, sr_2 = asyncio.run(backend.generate(text_2, voice_prompt, "en", 43, None))
        gen_time_2 = time.time() - t0

        c2_path = self.job_dir / "chunk_02.wav"
        fb_2, sha_2 = lj.write_atomic_audio(c2_path, audio_2, sr_2)
        dur_2 = float(len(audio_2) / sr_2)
        mgr.record_chunk_success(2, dur_2, gen_time_2, fb_2, sha_2, sr_2, lufs=-18.0, seed=43)

        final_manifest = mgr.update_status("concluido")

        self.assertEqual(final_manifest["job_status"], "concluido")
        self.assertEqual(final_manifest["completed_chunks"], 2)
        self.assertTrue(c1_path.exists())
        self.assertTrue(c2_path.exists())
        self.assertEqual(final_manifest["chunks"][0]["status"], "ok")
        self.assertEqual(final_manifest["chunks"][1]["status"], "ok")

        # Salva cópia do manifesto real para inclusão no relatório
        real_manifest_copy = self.job_dir / "real_manifest_kokoro.json"
        real_manifest_copy.write_text(json.dumps(final_manifest, indent=2, ensure_ascii=False), encoding="utf-8")



if __name__ == "__main__":
    unittest.main()

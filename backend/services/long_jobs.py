"""
Sistema de Persistência Transacional, Travas de Processo e Retomada para Texto Longo V2.

Módulo de serviço isolado e autocontido (Fase 3a da Missão de Texto Longo).
Responsável por:
- Trava exclusiva no nível do sistema operacional (job.lock fora da pasta do trabalho).
- Thread de pulso periódico independente (job.heartbeat.json) desacoplada do manifesto.
- Gerenciamento de ciclo de vida do manifesto (manifest.json) com escritor único sob lock.
- Gravação atômica com flush físico (os.fsync) e retry com backoff contra indexadores do Windows.
- Máquina de estados completa com detecção dinâmica de interrupção e sem-progresso.
- Retomada idempotente e preservação de tomadas anteriores na regeração de blocos.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import dataclasses
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
import uuid

import numpy as np
import soundfile as sf

try:
    import psutil
except ImportError:
    psutil = None  # type: ignore

try:
    import msvcrt
except ImportError:
    msvcrt = None  # type: ignore

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

logger = logging.getLogger("voicebox.long-jobs")

# Versão do esquema do manifesto
SCHEMA_VERSION = "2.0.0"

# Estados oficiais da máquina de estados
STATUS_PENDENTE = "pendente"
STATUS_EM_ANDAMENTO = "em_andamento"
STATUS_INTERROMPIDO = "interrompido"
STATUS_CANCELADO = "cancelado"
STATUS_FALHOU = "falhou"
STATUS_CONCLUIDO = "concluido"

# Transições válidas permitidas na máquina de estados
VALID_TRANSITIONS: Dict[str, Set[str]] = {
    STATUS_PENDENTE: {STATUS_EM_ANDAMENTO, STATUS_CANCELADO, STATUS_FALHOU, STATUS_INTERROMPIDO},
    STATUS_EM_ANDAMENTO: {STATUS_CONCLUIDO, STATUS_CANCELADO, STATUS_FALHOU, STATUS_INTERROMPIDO},
    STATUS_INTERROMPIDO: {STATUS_EM_ANDAMENTO, STATUS_CANCELADO},
    STATUS_CANCELADO: {STATUS_EM_ANDAMENTO},
    STATUS_FALHOU: {STATUS_EM_ANDAMENTO},
    # Na regeração ou remontagem de bloco, concluído pode voltar para em_andamento e retornar a concluído
    STATUS_CONCLUIDO: {STATUS_EM_ANDAMENTO},
}

# Estados elegíveis para retomada
RESUMABLE_STATES = {STATUS_PENDENTE, STATUS_INTERROMPIDO, STATUS_CANCELADO, STATUS_FALHOU}
# Concluído sem áudio final há mais que isto (s) = montagem que não terminou (ver inspect_job_state).
MONTAGEM_PRAZO_S = 600.0


# ---------------------------------------------------------------------------
# Exceções Personalizadas
# ---------------------------------------------------------------------------

class LongJobError(Exception):
    """Exceção base do subsistema de texto longo."""
    pass


class JobLockedError(LongJobError):
    """Lançada quando a trava do trabalho está ocupada por outro processo."""
    pass


class JobConfigConflictError(LongJobError):
    """Lançada quando parâmetros de retomada divergem do manifesto (HTTP 409)."""
    pass


class JobStateTransitionError(LongJobError):
    """Lançada quando uma transição de estado inválida é requisitada."""
    pass


# ---------------------------------------------------------------------------
# Resolução de Caminhos
# ---------------------------------------------------------------------------

def get_jobs_dir(data_dir: Path | str) -> Path:
    """Retorna o diretório base de trabalhos de texto longo."""
    p = Path(data_dir) / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def get_job_dir(data_dir: Path | str, job_id: str) -> Path:
    """Retorna o diretório da pasta do trabalho específico."""
    return get_jobs_dir(data_dir) / job_id


def get_job_lock_path(data_dir: Path | str, job_id: str) -> Path:
    """
    Retorna o caminho do arquivo de trava (fora da pasta renomeável).
    Fica em <data_dir>/jobs/<job_id>.lock, permitindo que a pasta <job_id>
    seja renomeada livremente no Windows enquanto a trava é retida.
    """
    return get_jobs_dir(data_dir) / f"{job_id}.lock"


def get_manifest_path(job_dir: Path | str) -> Path:
    """Retorna o caminho do arquivo manifest.json dentro da pasta do trabalho."""
    return Path(job_dir) / "manifest.json"


def get_heartbeat_path(job_dir: Path | str) -> Path:
    """Retorna o caminho do arquivo job.heartbeat.json dentro da pasta do trabalho."""
    return Path(job_dir) / "job.heartbeat.json"


# ---------------------------------------------------------------------------
# Trava Exclusiva no Nível do Sistema Operacional (JobLock)
# ---------------------------------------------------------------------------

class JobLock:
    """
    Gerencia a trava exclusiva no nível do sistema operacional para um trabalho.
    No Windows utiliza msvcrt.locking no primeiro byte com cursor em seek(0).
    No POSIX utiliza fcntl.flock com LOCK_EX | LOCK_NB.
    O handle permanece aberto durante toda a vida útil da geração.
    """

    def __init__(self, data_dir: Path | str, job_id: str):
        self.data_dir = Path(data_dir)
        self.job_id = job_id
        self.lock_path = get_job_lock_path(self.data_dir, job_id)
        self._handle: Optional[Any] = None

    def acquire(self) -> None:
        """Adquire a trava exclusiva não-bloqueante. Levanta JobLockedError se já ocupada."""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.lock_path, "a+b")
        handle.seek(0)

        if msvcrt is not None:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except (BlockingIOError, OSError) as exc:
                handle.close()
                raise JobLockedError(
                    f"Trabalho '{self.job_id}' está atualmente em execução por outro processo."
                ) from exc
        elif fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (BlockingIOError, OSError) as exc:
                handle.close()
                raise JobLockedError(
                    f"Trabalho '{self.job_id}' está atualmente em execução por outro processo."
                ) from exc

        self._handle = handle

    def release(self) -> None:
        """Destrava e fecha o descritor de arquivo."""
        if self._handle is None:
            return
        try:
            if msvcrt is not None:
                self._handle.seek(0)
                try:
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            elif fcntl is not None:
                try:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            try:
                self._handle.close()
            except OSError:
                pass
            self._handle = None

    def __enter__(self) -> JobLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.release()

    @classmethod
    def is_locked(cls, data_dir: Path | str, job_id: str) -> bool:
        """Verifica se a trava está retida por outro processo sem alterar estado."""
        lock_path = get_job_lock_path(data_dir, job_id)
        if not lock_path.exists():
            return False

        try:
            handle = open(lock_path, "a+b")
        except OSError:
            return True

        handle.seek(0)
        locked = False
        try:
            if msvcrt is not None:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    # Conseguiu travar; destrava imediatamente
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                except (BlockingIOError, OSError):
                    locked = True
            elif fcntl is not None:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (BlockingIOError, OSError):
                    locked = True
        finally:
            handle.close()

        return locked


# ---------------------------------------------------------------------------
# Verificação Segura de Processo (PID Liveness & Anti-Reciclagem)
# ---------------------------------------------------------------------------

def get_current_process_create_time() -> float:
    """Retorna o timestamp de criação do processo atual para prevenção de reciclagem de PID."""
    if psutil is not None:
        try:
            return float(psutil.Process().create_time())
        except Exception:
            pass
    return float(time.time())


def is_process_alive(pid: int, expected_create_time: Optional[float] = None) -> bool:
    """
    Verifica se o processo com o PID informado está vivo no sistema operacional.
    NUNCA chama os.kill(pid, 0) no Windows.
    Utiliza psutil com validação de horário de criação contra reaproveitamento de PID.
    Fallback puro com ctypes (OpenProcess) caso psutil não esteja acessível.
    """
    if pid <= 0:
        return False

    # 1. Caminho preferencial com psutil
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            if not proc.is_running() or proc.status() == psutil.STATUS_ZOMBIE:
                return False
            if expected_create_time is not None:
                # Tolerância de 1.5s para arredondamento de clock entre chamadas
                if abs(proc.create_time() - expected_create_time) > 1.5:
                    return False  # PID reaproveitado por outro processo
            return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return False
        except Exception as e:
            logger.debug(f"Erro ao checar processo via psutil: {e}")

    # 2. Fallback via Win32 ctypes (kernel32.dll)
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        exit_code = wintypes.DWORD()
        success = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        STILL_ACTIVE = 259
        return bool(success and exit_code.value == STILL_ACTIVE)

    # 3. Fallback genérico em sistemas POSIX
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Escrita Atômica Resiliente com Flushing e Backoff de Permissão
# ---------------------------------------------------------------------------

def write_atomic_json(
    target_path: Path,
    data: dict,
    max_attempts: int = 5,
    hook_env_var: Optional[str] = None,
) -> None:
    """
    Grava um dicionário JSON atomicamente via .tmp com flush(), os.fsync() e os.replace().
    Executa retry de até 5 tentativas com recuo exponencial em caso de PermissionError (Windows).
    Suporta gancho determinístico de teste via variável de ambiente.
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f"{target_path.stem}_{uuid.uuid4().hex[:8]}.tmp"

    serialized = json.dumps(data, indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(serialized)
        f.flush()
        os.fsync(f.fileno())

    # Gancho para testes determinísticos de interrupção
    if hook_env_var and os.environ.get(hook_env_var):
        hook_val = os.environ[hook_env_var]
        if hook_val == "pause":
            time.sleep(2.0)
        elif hook_val == "kill_self":
            os._exit(42)

    for attempt in range(max_attempts):
        try:
            os.replace(tmp_path, target_path)
            return
        except PermissionError:
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.05 * (2 ** attempt))


def write_atomic_audio(
    target_path: Path,
    audio: np.ndarray,
    sample_rate: int,
    max_attempts: int = 5,
    hook_env_var: Optional[str] = None,
) -> Tuple[int, str]:
    """
    Grava um arquivo Float32 WAV mono atomicamente com flush e os.replace().
    Retorna (tamanho_em_bytes, sha256_hex).
    """
    target_path = Path(target_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.parent / f"{target_path.stem}_{uuid.uuid4().hex[:8]}.tmp"

    audio_f32 = np.asarray(audio, dtype=np.float32)
    sf.write(str(tmp_path), audio_f32, sample_rate, subtype="FLOAT", format="WAV")

    with open(tmp_path, "a+b") as f:
        f.flush()
        os.fsync(f.fileno())

    # Gancho para testes determinísticos de interrupção no meio da gravação
    if hook_env_var and os.environ.get(hook_env_var):
        hook_val = os.environ[hook_env_var]
        if hook_val == "pause":
            time.sleep(2.0)
        elif hook_val == "kill_self":
            os._exit(43)

    for attempt in range(max_attempts):
        try:
            os.replace(tmp_path, target_path)
            break
        except PermissionError:
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.05 * (2 ** attempt))

    # Calcula métricas do arquivo promovido
    file_bytes = target_path.stat().st_size
    with open(target_path, "rb") as f:
        sha256 = hashlib.sha256(f.read()).hexdigest()

    return file_bytes, sha256


def read_json_with_retry(path: Path, max_attempts: int = 3) -> dict:
    """Lê um arquivo JSON com repetição suave caso coincida com momento de os.replace()."""
    path = Path(path)
    for attempt in range(max_attempts):
        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
                if content:
                    return json.loads(content)
        except (PermissionError, json.JSONDecodeError, FileNotFoundError):
            if attempt == max_attempts - 1:
                raise
        time.sleep(0.02 * (attempt + 1))
    raise RuntimeError(f"Falha ao ler {path}")


# ---------------------------------------------------------------------------
# Thread Dedicada de Pulso Periódico (HeartbeatWriter)
# ---------------------------------------------------------------------------

class HeartbeatWriter:
    """
    Thread daemon isolada que atualiza job.heartbeat.json periodicamente (a cada 5s).
    Captura QUALQUER exceção sem derrubar a thread.
    Se o replace falhar com PermissionError, simplesmente ignora o pulso atual.
    Grava: pid, create_time, last_beat, active_chunk_idx, chunk_started_at, last_progress_at.
    """

    def __init__(
        self,
        job_dir: Path | str,
        interval_s: float = 5.0,
        pid: Optional[int] = None,
        create_time: Optional[float] = None,
    ):
        self.job_dir = Path(job_dir)
        self.heartbeat_path = get_heartbeat_path(self.job_dir)
        self.interval_s = interval_s
        self.pid = pid or os.getpid()
        self.create_time = create_time or get_current_process_create_time()

        self.active_chunk_idx: int = 1
        self.chunk_started_at: float = time.time()
        self.last_progress_at: float = time.time()

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        """Inicia a thread daemon de pulso."""
        # Escreve o primeiro pulso imediatamente de forma síncrona
        self._write_pulse()
        self._thread = threading.Thread(target=self._run_loop, name=f"heartbeat-{self.job_dir.name}", daemon=True)
        self._thread.start()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            time.sleep(self.interval_s)
            if self._stop_event.is_set():
                break
            try:
                self._write_pulse()
            except Exception as e:
                logger.debug(f"Exceção capturada no loop de heartbeat: {e}")

    def _write_pulse(self) -> None:
        now = time.time()
        data = {
            "pid": self.pid,
            "create_time": self.create_time,
            "last_beat": now,
            "active_chunk_idx": self.active_chunk_idx,
            "chunk_started_at": self.chunk_started_at,
            "last_progress_at": self.last_progress_at,
            "status": "running",
        }
        try:
            write_atomic_json(
                self.heartbeat_path,
                data,
                max_attempts=1,
                hook_env_var="VOICEBOX_TEST_HOOK_HEARTBEAT_PRE_REPLACE",
            )
        except PermissionError:
            # Pula este pulso se o Windows/antivírus reter o descritor temporariamente
            logger.debug("Heartbeat pulado devido a PermissionError momentâneo.")
        except Exception as e:
            logger.debug(f"Falha ao escrever heartbeat: {e}")

    def advance_chunk(self, chunk_idx: int) -> None:
        """Atualiza a telemetria quando um novo bloco inicia sua geração."""
        now = time.time()
        self.active_chunk_idx = chunk_idx
        self.chunk_started_at = now
        self._write_pulse()

    def record_progress(self) -> None:
        """Registra que um bloco foi concluído com sucesso."""
        self.last_progress_at = time.time()
        self._write_pulse()

    def stop(self) -> None:
        """Encerra a thread de pulso com join seguro."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None


# ---------------------------------------------------------------------------
# Gestor do Manifesto (ManifestManager)
# ---------------------------------------------------------------------------

class ManifestManager:
    """
    Escritor único de manifest.json sob threading.Lock() interno.
    Garante que atualizações ocorram estritamente no início/fim de blocos e em mudanças de estado.
    """

    def __init__(self, job_dir: Path | str):
        self.job_dir = Path(job_dir)
        self.manifest_path = get_manifest_path(self.job_dir)
        self._lock = threading.Lock()

    def initialize_manifest(
        self,
        job_id: str,
        chunks_info: List[Dict[str, Any]],
        model_info: Dict[str, Any],
        voice_info: Dict[str, Any],
        language: str,
        original_text: str,
        generation_params: Dict[str, Any],
        abbreviations_version: str = "pt_v2.0_20261010",
        assembly_settings: Optional[Dict[str, Any]] = None,
    ) -> dict:
        """Cria e persiste o manifesto inicial do trabalho."""
        with self._lock:
            now = time.time()
            text_hash = hashlib.sha256(original_text.encode("utf-8")).hexdigest()

            # Config hash canônico cobrindo estritamente parâmetros que afetam a geração acústica
            canonical_config = {
                "model": {
                    "engine": model_info.get("engine"),
                    "model_size": model_info.get("model_size"),
                },
                "voice": {
                    "profile_id": voice_info.get("profile_id"),
                    "ref_audio_hash_sha256": voice_info.get("ref_audio_hash_sha256"),
                },
                "language": language,
                "original_text_hash_sha256": text_hash,
                "generation_parameters": {
                    "base_seed": generation_params.get("base_seed"),
                    "temperature": generation_params.get("temperature"),
                    "cfg": generation_params.get("cfg"),
                    "instruct": generation_params.get("instruct"),
                },
            }
            canonical_bytes = json.dumps(canonical_config, sort_keys=True, separators=(",", ":")).encode("utf-8")
            config_hash = hashlib.sha256(canonical_bytes).hexdigest()

            default_assembly = {
                "pause_oracao_ms": 120,
                "pause_frase_ms": 250,
                "pause_paragrafo_ms": 650,
                "target_lufs": -16.0,
                "max_gain_db": 8.0,
                "max_dbtp": -1.0,
                "crossfade_ms": 15,
                "audio_format": "FLOAT",
            }
            if assembly_settings:
                default_assembly.update(assembly_settings)

            # Monta os 16 campos rigorosos para cada bloco
            chunks_entries: List[Dict[str, Any]] = []
            for c in chunks_info:
                idx = int(c["idx"])
                entry = {
                    "idx": idx,
                    "paragraph_idx": int(c.get("paragraph_idx", 1)),
                    "boundary_type": str(c.get("boundary_type", "frase")),
                    "text": str(c["text"]),
                    "chars": len(str(c["text"])),
                    "status": STATUS_PENDENTE,
                    "wav_rel_path": f"chunk_{idx:02d}.wav",
                    "sha256": None,
                    "file_bytes": None,
                    "sample_rate": int(c.get("sample_rate", 24000)),
                    "audio_dur_s": None,
                    "gen_time_s": None,
                    "rtf": None,
                    "lufs": None,
                    "attempts": [],
                    "error_message": None,
                }
                chunks_entries.append(entry)

            manifest = {
                "schema_version": SCHEMA_VERSION,
                "generation_id": job_id,
                "v2": True,
                "job_status": STATUS_PENDENTE,
                "model": model_info,
                "voice": voice_info,
                "language": language,
                "generation_parameters": generation_params,
                "config_hash_sha256": config_hash,
                "original_text": original_text,
                "original_text_hash_sha256": text_hash,
                "abbreviations_list_version": abbreviations_version,
                "last_assembly_settings": default_assembly,
                "timestamps": {
                    "created_at": now,
                    "updated_at": now,
                    "finished_at": None,
                },
                "total_chunks": len(chunks_entries),
                "completed_chunks": 0,
                "total_chars": sum(c["chars"] for c in chunks_entries),
                "master_audio_path": None,
                "chunks": chunks_entries,
            }

            write_atomic_json(
                self.manifest_path,
                manifest,
                hook_env_var="VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE",
            )
            return manifest

    def create_initial_manifest(self, *args, **kwargs) -> dict:
        """Alias para initialize_manifest."""
        return self.initialize_manifest(*args, **kwargs)

    def load_manifest(self) -> dict:
        """Carrega o manifesto atual sob lock."""
        with self._lock:
            return read_json_with_retry(self.manifest_path)

    def get_manifest(self) -> dict:
        """Alias para load_manifest."""
        return self.load_manifest()

    def update_status(self, new_status: str, error_message: Optional[str] = None) -> dict:
        """Atualiza o estado global do trabalho validando transições permitidas."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            current_status = manifest.get("job_status", STATUS_PENDENTE)

            if current_status != new_status:
                allowed = VALID_TRANSITIONS.get(current_status, set())
                if new_status not in allowed:
                    raise JobStateTransitionError(
                        f"Transição inválida de '{current_status}' para '{new_status}'."
                    )

            now = time.time()
            manifest["job_status"] = new_status
            manifest["timestamps"]["updated_at"] = now
            if new_status in (STATUS_CONCLUIDO, STATUS_CANCELADO, STATUS_FALHOU):
                manifest["timestamps"]["finished_at"] = now
            if error_message:
                manifest["error"] = error_message

            write_atomic_json(
                self.manifest_path,
                manifest,
                hook_env_var="VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE",
            )
            return manifest

    def update_master_audio(self, master_audio_path: str) -> dict:
        """Registra o caminho final do áudio master e garante status concluido."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            now = time.time()
            manifest["master_audio_path"] = str(master_audio_path)
            manifest["job_status"] = STATUS_CONCLUIDO
            manifest["timestamps"]["updated_at"] = now
            if manifest["timestamps"].get("finished_at") is None:
                manifest["timestamps"]["finished_at"] = now
            write_atomic_json(
                self.manifest_path,
                manifest,
                hook_env_var="VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE",
            )
            return manifest

    def record_chunk_success(
        self,
        chunk_idx: int,
        audio_dur_s: float,
        gen_time_s: float,
        file_bytes: int,
        sha256: str,
        sample_rate: int,
        lufs: Optional[float] = None,
        seed: Optional[int] = None,
        wav_rel_path: Optional[str] = None,
    ) -> dict:
        """Registra a conclusão bem-sucedida de um bloco no manifesto."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            now = time.time()

            chunk = next((c for c in manifest["chunks"] if c["idx"] == chunk_idx), None)
            if chunk is None:
                raise ValueError(f"Bloco com índice {chunk_idx} não encontrado.")

            chunk["status"] = "ok"
            chunk["file_bytes"] = file_bytes
            chunk["sha256"] = sha256
            chunk["sample_rate"] = sample_rate
            chunk["audio_dur_s"] = round(audio_dur_s, 3)
            chunk["gen_time_s"] = round(gen_time_s, 3)
            chunk["rtf"] = round(gen_time_s / audio_dur_s, 2) if audio_dur_s > 0 else 0.0
            chunk["lufs"] = round(lufs, 2) if lufs is not None else None
            chunk["error_message"] = None
            # Áudio novo: a revisão do Whisper (Fase 7) era do áudio anterior.
            chunk.pop("revisao", None)
            if wav_rel_path:
                chunk["wav_rel_path"] = wav_rel_path

            attempt_idx = len(chunk.get("attempts", [])) + 1
            chunk.setdefault("attempts", []).append({
                "attempt_idx": attempt_idx,
                "seed": seed,
                "timestamp": now,
                "status": "ok",
                "wav_path": chunk["wav_rel_path"],
                "error": None,
            })

            # Atualiza contagem global
            manifest["completed_chunks"] = sum(1 for c in manifest["chunks"] if c["status"] == "ok")
            manifest["timestamps"]["updated_at"] = now

            write_atomic_json(
                self.manifest_path,
                manifest,
                hook_env_var="VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE",
            )
            return manifest

    def record_chunk_review(self, chunk_idx: int, revisao: dict) -> dict:
        """Guarda no bloco o resultado da revisão do Whisper (Fase 7)."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            chunk = next((c for c in manifest["chunks"] if c["idx"] == chunk_idx), None)
            if chunk is None:
                raise ValueError(f"Bloco com índice {chunk_idx} não encontrado.")
            chunk["revisao"] = revisao
            manifest["timestamps"]["updated_at"] = time.time()
            write_atomic_json(self.manifest_path, manifest)
            return manifest

    def record_review_summary(self, resumo: dict, revisoes: dict) -> dict:
        """Grava as marcas "ouvir" de todos os blocos e o resumo da revisão (Fase 7)."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            for c in manifest["chunks"]:
                if c["idx"] in revisoes:
                    c["revisao"] = revisoes[c["idx"]]
            manifest["revisao_resumo"] = resumo
            manifest["timestamps"]["updated_at"] = time.time()
            write_atomic_json(self.manifest_path, manifest)
            return manifest

    def record_chunk_failure(
        self,
        chunk_idx: int,
        error_msg: str,
        seed: Optional[int] = None,
    ) -> dict:
        """Registra a falha de um bloco no manifesto sem invalidar blocos anteriores."""
        with self._lock:
            manifest = read_json_with_retry(self.manifest_path)
            now = time.time()

            chunk = next((c for c in manifest["chunks"] if c["idx"] == chunk_idx), None)
            if chunk is None:
                raise ValueError(f"Bloco com índice {chunk_idx} não encontrado.")

            chunk["status"] = STATUS_FALHOU
            chunk["error_message"] = error_msg

            attempt_idx = len(chunk.get("attempts", [])) + 1
            chunk.setdefault("attempts", []).append({
                "attempt_idx": attempt_idx,
                "seed": seed,
                "timestamp": now,
                "status": STATUS_FALHOU,
                "error": error_msg,
            })

            manifest["timestamps"]["updated_at"] = now
            write_atomic_json(
                self.manifest_path,
                manifest,
                hook_env_var="VOICEBOX_TEST_HOOK_MANIFEST_PRE_REPLACE",
            )
            return manifest


# ---------------------------------------------------------------------------
# Regeração de Bloco com Preservação de Áudio Anterior (Adjustment 3)
# ---------------------------------------------------------------------------

def regenerate_chunk_file(
    job_dir: Path | str,
    chunk_idx: int,
    new_audio: np.ndarray,
    sample_rate: int,
    gen_time_s: float,
    seed: Optional[int] = None,
    lufs: Optional[float] = None,
) -> dict:
    """
    Grava o novo áudio do bloco regerado em temporário e só substitui o ativo se terminar com sucesso.
    O arquivo ativo anterior é preservado por RENOMEAÇÃO (ex: chunk_03.a1.wav).
    O manifesto aponta para o ativo e guarda o histórico de tentativas.
    """
    job_dir = Path(job_dir)
    manifest_mgr = ManifestManager(job_dir)
    manifest = manifest_mgr.load_manifest()

    chunk = next((c for c in manifest["chunks"] if c["idx"] == chunk_idx), None)
    if chunk is None:
        raise ValueError(f"Bloco {chunk_idx} inexistente no manifesto.")

    prev_attempts_count = len(chunk.get("attempts", []))
    archive_name = f"chunk_{chunk_idx:02d}.a{prev_attempts_count}.wav"
    active_name = f"chunk_{chunk_idx:02d}.wav"

    archive_path = job_dir / archive_name
    active_path = job_dir / active_name

    # Preserva o arquivo anterior renomeando-o se ele existir
    if active_path.exists():
        for attempt in range(5):
            try:
                os.replace(active_path, archive_path)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))

    # Grava o novo áudio no caminho ativo de forma atômica
    file_bytes, sha256 = write_atomic_audio(
        active_path,
        new_audio,
        sample_rate,
        hook_env_var="VOICEBOX_TEST_HOOK_BLOCK_PRE_REPLACE",
    )

    audio_dur_s = float(len(new_audio) / sample_rate)
    return manifest_mgr.record_chunk_success(
        chunk_idx=chunk_idx,
        audio_dur_s=audio_dur_s,
        gen_time_s=gen_time_s,
        file_bytes=file_bytes,
        sha256=sha256,
        sample_rate=sample_rate,
        lufs=lufs,
        seed=seed,
        wav_rel_path=active_name,
    )


# ---------------------------------------------------------------------------
# Inspeção Dinâmica de Estado e Detecção de "Possivelmente Travado"
# ---------------------------------------------------------------------------

def inspect_job_state(data_dir: Path | str, job_id: str) -> Dict[str, Any]:
    """
    Lê o manifesto, o heartbeat e o status de trava física para derivar o estado operacional.
    Detecta dinamicamente:
    - 'interrompido' se processo criador morreu ou pulso > 120s sem trava;
    - 'possivelmente_travado' se trava ocupada + pulso > 120s + PID vivo;
    - aviso 'sem_progresso' se nenhum bloco conclui após max(600s, 10x mediana).
    """
    data_dir = Path(data_dir)
    job_dir = get_job_dir(data_dir, job_id)

    if not job_dir.exists():
        return {"error": "Trabalho não encontrado", "exists": False}

    manifest_path = get_manifest_path(job_dir)
    if not manifest_path.exists():
        return {"error": "Manifesto inexistente", "exists": False}

    manifest = read_json_with_retry(manifest_path)
    current_status = manifest.get("job_status", STATUS_PENDENTE)

    # Lê heartbeat se existir
    heartbeat_path = get_heartbeat_path(job_dir)
    heartbeat_data: Optional[Dict[str, Any]] = None
    if heartbeat_path.exists():
        try:
            heartbeat_data = read_json_with_retry(heartbeat_path)
        except Exception:
            pass

    is_locked = JobLock.is_locked(data_dir, job_id)
    now = time.time()

    derived_status = current_status
    warning: Optional[str] = None

    if current_status == STATUS_EM_ANDAMENTO:
        if heartbeat_data:
            pid = heartbeat_data.get("pid", 0)
            create_time = heartbeat_data.get("create_time")
            last_beat = heartbeat_data.get("last_beat", 0)
            elapsed_beat = now - last_beat
            pid_alive = is_process_alive(pid, create_time)

            if is_locked and elapsed_beat > 120.0 and pid_alive:
                derived_status = "possivelmente_travado"
                warning = f"Processo (PID {pid}) ainda ativo no SO, mas sem pulso há {elapsed_beat:.1f}s."
            elif not is_locked or not pid_alive or elapsed_beat > 120.0:
                derived_status = STATUS_INTERROMPIDO
                warning = "Processo criador foi finalizado abruptamente."
            else:
                # Processo vivo e com pulso fresco. Checa telemetria de progresso (Adjustment 1)
                last_progress = heartbeat_data.get("last_progress_at", last_beat)
                durations = [c["gen_time_s"] for c in manifest.get("chunks", []) if c.get("gen_time_s")]
                median_dur = float(np.median(durations)) if durations else 20.0
                stagnation_thresh = max(600.0, 10.0 * median_dur)

                if (now - last_progress) > stagnation_thresh:
                    warning = "sem_progresso"
        else:
            if not is_locked:
                derived_status = STATUS_INTERROMPIDO

    elif current_status == STATUS_CONCLUIDO and not manifest.get("master_audio_path") and not is_locked:
        # Todos os blocos prontos, mas o áudio final nunca foi salvo (a montagem falhou ou o servidor fechou no meio).
        # Antes aparecia "Concluído" sem áudio e sem Retomar (varredura_geral.md, achado 3). Depois de
        # MONTAGEM_PRAZO_S sem pulso, vira "interrompido": Retomar só remonta (nenhum bloco é gerado de novo).
        ultimo = (heartbeat_data or {}).get("last_beat") or manifest_path.stat().st_mtime
        if now - float(ultimo) > MONTAGEM_PRAZO_S:
            derived_status = STATUS_INTERROMPIDO
            warning = "montagem_final_ausente"

    return {
        "job_id": job_id,
        "declared_status": current_status,
        "effective_status": derived_status,
        "is_locked": is_locked,
        "can_resume": derived_status in RESUMABLE_STATES,
        "warning": warning,
        "completed_chunks": manifest.get("completed_chunks", 0),
        "total_chunks": manifest.get("total_chunks", 0),
        "heartbeat": heartbeat_data,
        "manifest": manifest,
    }


# ---------------------------------------------------------------------------
# Validação e Planejamento da Retomada (Adjustment 4)
# ---------------------------------------------------------------------------

def get_pending_chunks(job_dir: Path | str, manifest: dict) -> List[int]:
    """
    Calcula quais blocos precisam ser gerados a partir do manifesto e dos arquivos em disco.
    Fonte da verdade: o manifesto.
    Um bloco precisa ser gerado se:
    - status != 'ok'
    - arquivo físico .wav não existe
    - sha256 do arquivo físico diverge do registrado no bloco
    """
    job_dir = Path(job_dir)
    chunks_to_generate: List[int] = []
    for c in manifest.get("chunks", []):
        idx = c["idx"]
        status = c.get("status")
        rel_path = c.get("wav_rel_path", f"chunk_{idx:02d}.wav")
        full_path = job_dir / rel_path

        if status != "ok":
            chunks_to_generate.append(idx)
        elif not full_path.exists():
            chunks_to_generate.append(idx)
        else:
            try:
                with open(full_path, "rb") as f:
                    file_sha = hashlib.sha256(f.read()).hexdigest()
                if file_sha != c.get("sha256"):
                    chunks_to_generate.append(idx)
            except OSError:
                chunks_to_generate.append(idx)

    return chunks_to_generate


def validate_resume_plan(
    data_dir: Path | str,
    job_id: str,
    request_data: Optional[Dict[str, Any]] = None,
) -> Tuple[dict, List[int]]:
    """
    Valida se um trabalho pode ser retomado e calcula os blocos pendentes.
    Corpo de requisição opcional (se None ou vazio, utiliza os parâmetros do manifesto).
    Compara estritamente apenas os campos explicitamente informados (409 Conflict se divergirem).
    Retorna (manifesto, lista_de_indices_a_gerar).
    NUNCA re-divide o texto: usa os textos guardados no manifesto.
    """
    state_info = inspect_job_state(data_dir, job_id)
    if not state_info.get("manifest"):
        raise LongJobError("Trabalho ou manifesto inexistente.")

    if not state_info["can_resume"]:
        eff = state_info["effective_status"]
        if eff == "possivelmente_travado":
            raise JobLockedError("Trabalho está possivelmente travado. Aguarde ou confirme liberação.")
        if eff == STATUS_EM_ANDAMENTO:
            raise JobLockedError("Trabalho já está em andamento.")
        if eff == STATUS_CONCLUIDO:
            raise LongJobError("Trabalho já foi concluído com sucesso.")
        raise JobLockedError(f"Trabalho em estado '{eff}' não permite retomada.")

    manifest = state_info["manifest"]
    job_dir = get_job_dir(data_dir, job_id)

    # Validação do schema_version
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise JobConfigConflictError(
            f"Versão de esquema incompatível: esperada '{SCHEMA_VERSION}', encontrada '{manifest.get('schema_version')}'."
        )

    # Compara estritamente campos enviados pelo cliente (Adjustment 4)
    if request_data:
        # 1. Texto de entrada
        if "text" in request_data and request_data["text"] is not None:
            req_text_hash = hashlib.sha256(request_data["text"].encode("utf-8")).hexdigest()
            if req_text_hash != manifest.get("original_text_hash_sha256"):
                raise JobConfigConflictError("O texto fornecido diverge do texto original do trabalho.")

        # 2. Modelo
        if "engine" in request_data and request_data["engine"] is not None:
            if request_data["engine"] != manifest.get("model", {}).get("engine"):
                raise JobConfigConflictError("O motor do modelo diverge do manifesto.")
        if "model_size" in request_data and request_data["model_size"] is not None:
            if request_data["model_size"] != manifest.get("model", {}).get("model_size"):
                raise JobConfigConflictError("O tamanho do modelo diverge do manifesto.")

        # 3. Perfil de voz
        if "profile_id" in request_data and request_data["profile_id"] is not None:
            if request_data["profile_id"] != manifest.get("voice", {}).get("profile_id"):
                raise JobConfigConflictError("O identificador do perfil de voz diverge do manifesto.")

        # 4. Hash do áudio de referência
        if "ref_audio_hash_sha256" in request_data and request_data["ref_audio_hash_sha256"] is not None:
            if request_data["ref_audio_hash_sha256"] != manifest.get("voice", {}).get("ref_audio_hash_sha256"):
                raise JobConfigConflictError("O áudio de referência da voz foi alterado desde o início do trabalho.")

        # 5. Idioma
        if "language" in request_data and request_data["language"] is not None:
            if request_data["language"] != manifest.get("language"):
                raise JobConfigConflictError("O idioma diverge do manifesto.")

    # Calcula quais blocos precisam ser gerados
    # Fonte da verdade: o manifesto. Arquivo sem status 'ok' ou com SHA inválido é ignorado.
    chunks_to_generate = get_pending_chunks(job_dir, manifest)
    return manifest, chunks_to_generate


# ---------------------------------------------------------------------------
# Renomeação Segura de Pasta para /retry (Adjustment 2)
# ---------------------------------------------------------------------------

def rename_job_dir_for_retry(
    data_dir: Path | str,
    job_id: str,
    acquired_lock: Optional[JobLock] = None,
) -> Path:
    """
    Renomeia a pasta jobs/<id>/ para jobs/<id>_retry_YYYYMMDD_HHMMSS/ no Windows.
    Só ocorre com a trava jobs/<id>.lock previamente adquirida.
    Devolve JobLockedError (HTTP 409) se a trava estiver ocupada por outro processo.
    """
    data_dir = Path(data_dir)
    job_dir = get_job_dir(data_dir, job_id)
    if not job_dir.exists():
        return job_dir

    lock = acquired_lock or JobLock(data_dir, job_id)
    own_lock = False
    if acquired_lock is None:
        try:
            lock.acquire()
            own_lock = True
        except JobLockedError as exc:
            raise JobLockedError(
                f"Não é possível reiniciar o trabalho '{job_id}' porque ele está atualmente em execução."
            ) from exc

    try:
        timestamp_str = time.strftime("%Y%m%d_%H%M%S")
        target_path = data_dir / "jobs" / f"{job_id}_retry_{timestamp_str}"
        for attempt in range(5):
            try:
                os.rename(job_dir, target_path)
                return target_path
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (2 ** attempt))
        return target_path
    finally:
        if own_lock:
            lock.release()


# ---------------------------------------------------------------------------
# Laço Oficial de Produção (run_job)
# ---------------------------------------------------------------------------

class JobCancelledError(Exception):
    """Exceção levantada quando um trabalho é cancelado cooperativamente."""
    pass


def run_job(
    data_dir: Path | str,
    job_id: str,
    synth_fn: Callable[..., Tuple[np.ndarray, int]],
    pulse_interval_s: float = 5.0,
    hook_env_var: Optional[str] = None,
    is_cancelled_fn: Optional[Callable[[], bool]] = None,
    chunk_completed_hook: Optional[Callable[[int, dict], None]] = None,
) -> dict:
    """
    Executa o laço de produção oficial para geração de áudio de um trabalho longo.

    1. Idempotência rápida: se o trabalho já está 'concluido', retorna o manifesto imediatamente.
    2. Adquire trava exclusiva do SO (JobLock). Se ocupada, levanta JobLockedError.
    3. Inicia thread desacoplada de pulso (HeartbeatWriter).
    4. Sob trava, relê o manifesto fresco e recalcula a lista de blocos pendentes (get_pending_chunks).
    5. Se não houver blocos pendentes, garante status 'concluido' e retorna.
    6. Atualiza status para 'em_andamento'.
    7. Itera sobre blocos pendentes:
       - Checa cancelamento cooperativo -> atualiza para STATUS_CANCELADO e encerra.
       - Pulsa heartbeat.advance_chunk(idx).
       - Invoca synth_fn(chunk_info, seed).
       - Grava áudio atomicamente com fsync (write_atomic_audio).
       - Registra sucesso (record_chunk_success).
       - Pulsa heartbeat.record_progress().
       - Executa chunk_completed_hook se fornecido.
    8. Mapeamento de Exceções:
       - OSError (errno 28 / ENOSPC / disco cheio):
         Marca bloco como 'falhou', trabalho como STATUS_INTERROMPIDO e encerra sem relançar.
       - JobCancelledError:
         Marca trabalho como STATUS_CANCELADO e encerra sem relançar.
       - Exceções inesperadas:
         Marca bloco como 'falhou', trabalho como STATUS_FALHOU e relança a exceção.
    9. Conclusão: se todos os blocos completarem, marca STATUS_CONCLUIDO.
    10. Bloco finally garante encerramento do pulso e liberação da trava do SO.
    """
    data_dir = Path(data_dir)
    job_dir = get_job_dir(data_dir, job_id)
    mgr = ManifestManager(job_dir)

    # 1. Idempotência rápida: trabalho já concluído E com áudio master retorna imediatamente
    initial_manifest = mgr.load_manifest()
    if initial_manifest.get("job_status") == STATUS_CONCLUIDO and initial_manifest.get("master_audio_path"):
        return initial_manifest

    # 2. Adquire trava exclusiva do SO (JobLock). Se ocupada por outro processo, levanta JobLockedError.
    lock = JobLock(data_dir, job_id)
    lock.acquire()

    hb = HeartbeatWriter(job_dir, interval_s=pulse_interval_s)

    try:
        hb.start()

        # 3. RELEITURA NOVA DO MANIFESTO E CÁLCULO DE BLOCOS PENDENTES SOB TRAVA
        # Garante idempotência e que nenhuma decisão de blocos pendentes seja tomada antes da trava
        manifest = mgr.load_manifest()
        pending_chunks = get_pending_chunks(job_dir, manifest)

        if not pending_chunks:
            if manifest.get("job_status") != STATUS_CONCLUIDO:
                mgr.update_status(STATUS_CONCLUIDO)
            return mgr.load_manifest()

        mgr.update_status(STATUS_EM_ANDAMENTO)
        base_seed = (manifest.get("generation_parameters") or manifest.get("generation_params") or {}).get("base_seed")

        sig = inspect.signature(synth_fn)
        num_params = len(sig.parameters)

        for idx in pending_chunks:
            if is_cancelled_fn and is_cancelled_fn():
                mgr.update_status(STATUS_CANCELADO)
                return mgr.get_manifest()

            chunk = next(c for c in manifest["chunks"] if c["idx"] == idx)
            seed = chunk.get("seed") or (base_seed + (idx - 1) if base_seed is not None else None)

            hb.advance_chunk(idx)
            t0 = time.time()

            try:
                if num_params == 1:
                    audio, sr = synth_fn(chunk)
                else:
                    audio, sr = synth_fn(chunk, seed)

                g_time = time.time() - t0
                # Revisão do Whisper (Fase 7) pode ter trocado a semente ao refazer o bloco.
                seed = chunk.pop("_semente_usada", seed)
                c_path = job_dir / f"chunk_{idx:02d}.wav"
                fb, sha = write_atomic_audio(c_path, audio, sr, hook_env_var=hook_env_var)
                dur = float(len(audio) / sr)
                mgr.record_chunk_success(idx, dur, g_time, fb, sha, sr, seed=seed)
                hb.record_progress()

                if chunk_completed_hook:
                    chunk_completed_hook(idx, chunk)

            except OSError as exc:
                is_disk_full = (
                    getattr(exc, "errno", None) in (28, 122)
                    or "space" in str(exc).lower()
                    or "enospc" in str(exc).lower()
                )
                if is_disk_full:
                    mgr.record_chunk_failure(idx, str(exc))
                    mgr.update_status(STATUS_INTERROMPIDO, error_message=str(exc))
                    return mgr.get_manifest()
                else:
                    mgr.record_chunk_failure(idx, str(exc))
                    mgr.update_status(STATUS_FALHOU, error_message=str(exc))
                    raise

            except JobCancelledError:
                mgr.update_status(STATUS_CANCELADO)
                return mgr.get_manifest()

            except Exception as exc:
                mgr.record_chunk_failure(idx, str(exc))
                mgr.update_status(STATUS_FALHOU, error_message=str(exc))
                raise

        mgr.update_status(STATUS_CONCLUIDO)
        return mgr.get_manifest()

    finally:
        try:
            hb.stop()
        except Exception:
            pass
        try:
            lock.release()
        except Exception:
            pass


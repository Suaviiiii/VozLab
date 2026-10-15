#!/usr/bin/env python3
"""
Serviço lateral local do OmniVoice para integração com o Voicebox.
Executa em processo e ambiente Python 100% isolados (pasta .venv do OmniVoice).
Porta: 17495 (apenas 127.0.0.1)

Missão OmniVoice, Fase OV-2 (14/10/2026):
- este processo NÃO usa a GPU. Quem carrega o modelo é o trabalhador (omnivoice_trabalhador.py, processo filho);
- uma fila única: um pedido por vez, na ordem de chegada (VozLab e painel nativo);
- liberar a placa = encerrar o trabalhador (0 MB de verdade);
- árbitro: antes de carregar o modelo, pede a placa ao VozLab; o VozLab pede a placa por /gpu/ceder;
- descarrega sozinho após N minutos parado (config_servico.json, "inatividade_min");
- o painel nativo (Gradio) fica montado em /painel, usando a mesma fila e o mesmo modelo;
- a porta antiga do painel (7860) vira um atalho que leva a /painel.
As rotas antigas (/health, /modes, /generate, /status, /audio, /cancel, /unload, /load, /models) mantêm nome e formato.
"""

import os
import sys
import json
import time
import uuid
import queue
import logging
import threading
from pathlib import Path
from typing import Optional, Dict, Any

# Configurações de GPU AMD Radeon RX 7600 (gfx1102 / ROCm) — herdadas pelo trabalhador
os.environ["HSA_OVERRIDE_GFX_VERSION"] = "11.0.0"
os.environ["HIP_VISIBLE_DEVICES"] = "0"

import numpy as np
import soundfile as sf
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("omnivoice_service")

app = FastAPI(title="OmniVoice Local Service", version="0.2.1")


# Trava de origem (15/10/2026), igual à do VozLab: um site aberto no navegador não consegue mandar
# comandos para este serviço (carregar, descarregar, cancelar, gerar). Pedidos sem Origin (o VozLab e
# scripts locais) e os da própria tela (mesmo endereço ou a tela do VozLab) continuam valendo.
_HOSTS_OK = {"127.0.0.1", "localhost", "[::1]"} | {
    h.strip().lower() for h in os.environ.get("OMNIVOICE_ALLOWED_HOSTS", "").split(",") if h.strip()}
_ORIGENS_OK = {"http://127.0.0.1:17493", "http://localhost:17493"}


@app.middleware("http")
async def _trava_de_origem(request, call_next):
    from fastapi.responses import JSONResponse

    host = (request.headers.get("host") or "").lower()
    nome = host.rsplit(":", 1)[0] if not host.startswith("[") else host.split("]")[0] + "]"
    if host and nome not in _HOSTS_OK:
        return JSONResponse({"detail": "Host não permitido"}, status_code=403)
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origem = request.headers.get("origin")
        if origem is not None and origem not in _ORIGENS_OK and origem not in (f"http://{host}", f"https://{host}"):
            return JSONResponse({"detail": "Origem não permitida"}, status_code=403)
    return await call_next(request)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "outputs" / "service_generations"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
PROMPT_CACHE_DIR = BASE_DIR / "cache_prompts"
PROMPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = BASE_DIR / "config_servico.json"
ESTADO_GPU_FILE = BASE_DIR / "outputs" / "estado_gpu.json"

MODEL_NAME = "k2-fsa/OmniVoice"
PORTA = 17495

CONFIG_PADRAO = {
    "inatividade_min": 10,           # 0 = nunca descarrega sozinho
    "vozlab_url": "http://127.0.0.1:17493",
    "espera_vozlab_max_s": 7200,     # quanto esperar o VozLab terminar (um texto longo pode passar de 45 min)
    "montar_painel": True,           # painel nativo em /painel
    "porta_painel_antiga": 7860,     # 0 = sem atalho na porta antiga
    "montar_painel_original": False, # True = o painel original (inglês/chinês) também em /painel-original
}


def ler_config() -> Dict[str, Any]:
    cfg = dict(CONFIG_PADRAO)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning(f"config_servico.json inválido ({e}); usando o padrão.")
    if os.environ.get("OMNIVOICE_VOZLAB_URL"):  # testes: VozLab temporário em outra porta
        cfg["vozlab_url"] = os.environ["OMNIVOICE_VOZLAB_URL"]
    if os.environ.get("OMNIVOICE_INATIVIDADE_MIN"):  # testes: tempo de inatividade curto
        cfg["inatividade_min"] = float(os.environ["OMNIVOICE_INATIVIDADE_MIN"])
    return cfg


_jobs: Dict[str, Dict[str, Any]] = {}


# =====================================================================================
# Trabalhador de GPU (processo filho)
# =====================================================================================
class Trabalhador:
    def __init__(self):
        self.proc = None
        self.con = None
        self.cancelar = None  # multiprocessing.Event: para a geração entre dois passos (OV-3)
        self.info: Dict[str, Any] = {}
        try:
            self.info = json.loads(ESTADO_GPU_FILE.read_text(encoding="utf-8"))
        except Exception:
            self.info = {}

    def vivo(self) -> bool:
        return self.proc is not None and self.proc.is_alive()

    def iniciar(self):
        import multiprocessing as mp
        from omnivoice_trabalhador import principal
        ctx = mp.get_context("spawn")
        lado_servico, lado_trabalhador = ctx.Pipe()
        self.cancelar = ctx.Event()
        self.proc = ctx.Process(target=principal, args=(lado_trabalhador, self.cancelar),
                                name="omnivoice_trabalhador", daemon=True)
        self.proc.start()
        self.con = lado_servico
        logger.info(f"Trabalhador de GPU iniciado (pid {self.proc.pid}).")

    def pedir(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        try:
            self.con.send(msg)
            return self.con.recv()
        except (EOFError, OSError, BrokenPipeError) as e:
            self.encerrar()
            raise RuntimeError(f"o trabalhador de GPU parou inesperadamente ({e})")

    def carregar(self) -> Dict[str, Any]:
        if not self.vivo():
            self.iniciar()
        r = self.pedir({"tipo": "carregar"})
        if not r.get("ok"):
            raise RuntimeError(r.get("erro"))
        self.info = {"device": r["device"], "gpu_available": r["gpu_available"], "vram_mb": r["vram_mb"],
                     "sampling_rate": r["sampling_rate"], "dtype": r.get("dtype")}
        try:
            ESTADO_GPU_FILE.write_text(json.dumps(self.info), encoding="utf-8")
        except Exception:
            pass
        return r

    def gerar(self, pedido: Dict[str, Any]) -> Dict[str, Any]:
        """Gera um pedido. O sinal de cancelamento é zerado pelo despachante ANTES de o pedido virar o atual
        (zerar aqui apagaria um cancelamento que chegou enquanto o modelo carregava)."""
        return self.pedir({"tipo": "gerar", "pedido": pedido})

    def encerrar(self) -> bool:
        """Encerra o processo (e os filhos dele). É o único jeito de a placa voltar a 0 MB."""
        if self.proc is None:
            return False
        estava_vivo = self.proc.is_alive()
        try:
            import psutil
            p = psutil.Process(self.proc.pid)
            for c in p.children(recursive=True):
                c.kill()
            p.kill()
        except Exception:
            pass
        try:
            self.proc.join(timeout=15)
        except Exception:
            pass
        try:
            self.con.close()
        except Exception:
            pass
        self.proc, self.con = None, None
        if estava_vivo:
            logger.info("Trabalhador de GPU encerrado: placa liberada.")
        return estava_vivo


_trab = Trabalhador()
_trava = threading.Lock()          # quem segura usa o trabalhador (uma geração, um load ou um unload)
_fila: "queue.Queue[Dict[str, Any]]" = queue.Queue()
_estado = {"executando": None, "aguardando": None, "ultima_atividade": time.time(), "na_fila": 0}
_cancelados_na_fila: set = set()   # pedidos /v2 cancelados antes de começar
_forcados: set = set()             # pedidos parados encerrando o trabalhador (cancelamento forte)
CANCELAR_FORTE_APOS_S = 3.0


def _sinalizar_cancelamento(pedido_id: str):
    """Suave: sinal entre passos. Forte: se em 3 s o pedido ainda for o atual, encerra o trabalhador.
    (Depois do último passo, o modelo transforma os códigos em som numa etapa só, que não tem onde parar.)"""
    if _trab.cancelar is not None:
        _trab.cancelar.set()
    logger.info(f"Cancelamento pedido para {pedido_id} (gerando): sinal enviado ao trabalhador.")

    def _forte():
        time.sleep(CANCELAR_FORTE_APOS_S)
        if _estado["executando"] == pedido_id:
            logger.info(f"{pedido_id} não parou em {CANCELAR_FORTE_APOS_S:.0f}s: encerrando o trabalhador "
                        f"(cancelamento forte; o modelo recarrega no próximo pedido).")
            _forcados.add(pedido_id)
            _trab.encerrar()

    threading.Thread(target=_forte, name=f"cancelar_{pedido_id}", daemon=True).start()


def cancelar_pedido(pedido_id: str) -> str:
    """Cancela um pedido: se ainda está na fila, é pulado; se está gerando, para no passo seguinte ou,
    em até 3 s, encerrando o trabalhador."""
    if _estado["executando"] == pedido_id:
        _sinalizar_cancelamento(pedido_id)
        return "gerando"
    _cancelados_na_fila.add(pedido_id)
    return "na_fila"


# =====================================================================================
# Árbitro da GPU (lado OmniVoice)
# =====================================================================================
def pedir_placa_ao_vozlab(rotulo: str):
    """Antes de carregar o modelo: pede ao VozLab que ceda a placa. Nunca corta uma geração do VozLab."""
    import httpx
    cfg = ler_config()
    url = cfg["vozlab_url"].rstrip("/") + "/omnivoice/gpu/ceder"
    t0 = time.time()
    avisou = False
    while True:
        try:
            r = httpx.post(url, json={"quem": "omnivoice"}, timeout=3.0)
            res = r.json() if r.status_code == 200 else {"resultado": "sem_arbitro"}
        except Exception:
            res = {"resultado": "desligado"}
        if res.get("resultado") != "ocupado":
            if avisou:
                logger.info(f"VozLab cedeu a placa ({res.get('resultado')}) após {time.time() - t0:.0f}s.")
            _estado["aguardando"] = None
            return res
        if not avisou:
            logger.info(f"Aguardando o VozLab terminar a geração dele para usar a placa ({rotulo})...")
            avisou = True
        _estado["aguardando"] = "vozlab"
        if time.time() - t0 > cfg["espera_vozlab_max_s"]:
            _estado["aguardando"] = None
            raise RuntimeError("o VozLab ocupou a placa por tempo demais; tente de novo quando ele terminar")
        time.sleep(2)


def _garantir_modelo(rotulo: str) -> float:
    """Sobe o trabalhador e carrega o modelo, pedindo a placa ao VozLab antes. Devolve o tempo de carga."""
    if _trab.vivo():
        return 0.0
    pedir_placa_ao_vozlab(rotulo)
    t0 = time.time()
    _trab.carregar()
    return time.time() - t0


def _executar(tarefa: Dict[str, Any]):
    tipo = tarefa["tipo"]
    if tipo == "servico":
        job_id = tarefa["job_id"]
        if _jobs[job_id]["status"] == "cancelled":
            logger.info(f"Job {job_id} cancelado antes de começar: pulado.")
            return
        try:
            if not _trab.vivo():
                pedir_placa_ao_vozlab(job_id)   # a espera pelo VozLab não entra no tempo da geração
            t0 = time.time()
            if not _trab.vivo():
                _trab.carregar()
            r = _trab.gerar(tarefa["pedido"])
            if r.get("cancelado"):
                _jobs[job_id]["status"] = "cancelled"
                logger.info(f"Job {job_id} cancelado durante a geração ({r.get('segundos')}s).")
                return
            if not r.get("ok"):
                raise RuntimeError(r.get("erro"))
            elapsed = time.time() - t0
            onda, sr = r["audio"], r["sampling_rate"]
            out_file = OUTPUT_DIR / f"{job_id}.wav"
            sf.write(str(out_file), onda, sr)
            duracao = len(onda) / sr
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["duration_sec"] = round(duracao, 2)
            _jobs[job_id]["elapsed_sec"] = round(elapsed, 2)
            _jobs[job_id]["audio_path"] = str(out_file)
            _jobs[job_id]["audio_url"] = f"/audio/{job_id}"
            logger.info(f"Job {job_id} concluído com sucesso: {duracao:.2f}s de áudio gerados em {elapsed:.2f}s.")
        except Exception as e:
            if job_id in _forcados:
                _forcados.discard(job_id)
                _jobs[job_id]["status"] = "cancelled"
                logger.info(f"Job {job_id} cancelado (trabalhador encerrado).")
                return
            logger.error(f"Erro no job {job_id}: {e}", exc_info=True)
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(e)
    elif tipo in ("painel", "carregar", "bloco"):
        try:
            if tipo == "bloco" and tarefa.get("pedido_id") in _cancelados_na_fila:
                _cancelados_na_fila.discard(tarefa["pedido_id"])
                tarefa["resposta"] = {"ok": False, "cancelado": True, "erro": "cancelado antes de começar"}
                return
            carga = _garantir_modelo(tipo)
            if tipo == "carregar":
                tarefa["resposta"] = {"ok": True, "segundos": carga}
            else:
                r = _trab.gerar(tarefa["pedido"])
                r["segundos_carga"] = round(carga, 2)
                tarefa["resposta"] = r
        except Exception as e:
            if tarefa.get("pedido_id") in _forcados:
                _forcados.discard(tarefa["pedido_id"])
                tarefa["resposta"] = {"ok": False, "cancelado": True, "erro": "cancelado (trabalhador encerrado)"}
                logger.info(f"{tarefa['pedido_id']} cancelado (trabalhador encerrado).")
            else:
                logger.error(f"Erro ({tipo}): {e}", exc_info=True)
                tarefa["resposta"] = {"ok": False, "erro": str(e)}
        finally:
            tarefa["pronto"].set()


def _despachante():
    """Fila única: um pedido por vez, na ordem de chegada."""
    while True:
        tarefa = _fila.get()
        with _trava:
            if _trab.cancelar is not None:
                _trab.cancelar.clear()  # zera ANTES de o pedido virar o atual (um cancelamento depois disto vale)
            _estado["executando"] = tarefa.get("job_id") or tarefa.get("pedido_id") or tarefa["tipo"]
            _estado["na_fila"] = _fila.qsize()
            try:
                _executar(tarefa)
            finally:
                _estado["executando"] = None
                _estado["ultima_atividade"] = time.time()
                _estado["na_fila"] = _fila.qsize()


def _vigia_inatividade():
    """Descarrega (encerra o trabalhador) depois de N minutos sem pedidos."""
    while True:
        time.sleep(5)
        minutos = ler_config().get("inatividade_min", 10) or 0
        if minutos <= 0 or not _trab.vivo() or not _fila.empty():
            continue
        if time.time() - _estado["ultima_atividade"] < minutos * 60:
            continue
        if _trava.acquire(blocking=False):
            try:
                if _trab.vivo() and _fila.empty():
                    logger.info(f"{minutos} min sem pedidos: descarregando o OmniVoice da placa.")
                    _trab.encerrar()
            finally:
                _trava.release()


def _enfileirar_e_esperar(tarefa: Dict[str, Any], limite: Optional[float] = None) -> Dict[str, Any]:
    tarefa["pronto"] = threading.Event()
    _fila.put(tarefa)
    _estado["na_fila"] = _fila.qsize()
    tarefa["pronto"].wait(limite)
    return tarefa.get("resposta") or {"ok": False, "erro": "tempo esgotado esperando a fila"}


# =====================================================================================
# Rotas antigas (mesmo nome, mesmo formato)
# =====================================================================================
class GenerateRequest(BaseModel):
    mode: str = Field(default="clone", description="'clone', 'design' ou 'auto'")
    text: str = Field(..., min_length=1, description="Texto a ser falado")
    language: Optional[str] = Field(default=None, description="Código de idioma (ex: 'pt', 'en') ou null para auto")
    ref_audio_path: Optional[str] = Field(default=None, description="Caminho do áudio de referência")
    ref_text: Optional[str] = Field(default=None, description="Transcrição da referência")
    instruct: Optional[str] = Field(default=None, description="Instruções de tom/sotaque/atributos")
    num_step: int = Field(default=16, ge=4, le=64, description="Passos de difusão (padrão equilibrado: 16)")
    guidance_scale: float = Field(default=2.0, ge=0.0, le=4.0)
    speed: float = Field(default=1.0, ge=0.5, le=1.5)
    duration: Optional[float] = Field(default=None)
    denoise: bool = Field(default=True)
    preprocess_prompt: bool = Field(default=True)
    postprocess_output: bool = Field(default=True)
    # OV-3 (opcionais; sem eles, tudo como antes)
    seed: Optional[int] = Field(default=None, description="Semente: mesmo texto + mesma semente = mesmo áudio")
    t_shift: Optional[float] = Field(default=None, description="Deslocamento dos passos (padrão do modelo 0,1)")
    position_temperature: Optional[float] = Field(default=None, description="Temperatura da escolha de posição (padrão 5)")
    class_temperature: Optional[float] = Field(default=None, description="Temperatura do sorteio de som (padrão 0)")
    layer_penalty_factor: Optional[float] = Field(default=None, description="Penalidade por camada (padrão 5)")
    audio_chunk_duration: Optional[float] = Field(default=None, description="Divisão interna: tamanho (padrão 15 s)")
    audio_chunk_threshold: Optional[float] = Field(default=None, description="Divisão interna: a partir de (padrão 30 s)")


@app.get("/health")
def health():
    vivo = _trab.vivo()
    return {
        "status": "online",
        "model_loaded": vivo,
        "device": _trab.info.get("device", "desconhecido (ainda não carregado)"),
        "gpu_available": bool(_trab.info.get("gpu_available", False)),
        "vram_used_mb": float(_trab.info.get("vram_mb", 0.0)) if vivo else 0.0,
        "version": "0.2.1"
    }


@app.get("/modes")
def get_modes():
    return {
        "modes": [
            {"id": "clone", "label": "Clonagem de Voz", "requires_reference": True},
            {"id": "design", "label": "Design de Voz", "requires_reference": False},
            {"id": "auto", "label": "Voz Automática", "requires_reference": False}
        ],
        "default_steps": 16,
        "supported_languages": 600
    }


@app.post("/generate")
def generate(req: GenerateRequest):
    job_id = f"ov_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    _jobs[job_id] = {
        "job_id": job_id,
        "status": "processing",
        "created_at": time.time(),
        "error": None
    }
    lang = req.language if (req.language and req.language.lower() != "auto") else None
    pedido = req.model_dump() if hasattr(req, "model_dump") else req.dict()
    pedido["language"] = lang
    _fila.put({"tipo": "servico", "job_id": job_id, "pedido": pedido})
    _estado["na_fila"] = _fila.qsize()
    return {"job_id": job_id, "status": "processing"}


@app.get("/status/{job_id}")
def get_status(job_id: str):
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job não encontrado")
    return _jobs[job_id]


@app.get("/audio/{job_id}")
def get_audio(job_id: str):
    if job_id not in _jobs or _jobs[job_id]["status"] != "completed":
        raise HTTPException(status_code=404, detail="Áudio não disponível")
    audio_path = _jobs[job_id]["audio_path"]
    if not os.path.exists(audio_path):
        raise HTTPException(status_code=404, detail="Arquivo de áudio não encontrado no disco")
    return FileResponse(audio_path, media_type="audio/wav", filename=f"{job_id}.wav")


@app.post("/cancel/{job_id}")
def cancel(job_id: str):
    # Um pedido na fila é pulado; um que está gerando para no passo seguinte (OV-3). Mesma resposta de antes.
    if job_id in _jobs and _jobs[job_id]["status"] == "processing":
        _jobs[job_id]["status"] = "cancelled"
        if _estado["executando"] == job_id:
            _sinalizar_cancelamento(job_id)
        return {"cancelled": True}
    return {"cancelled": False}


@app.post("/unload")
def unload_model():
    # Espera a geração atual terminar (nunca corta) e encerra o trabalhador: a placa volta a 0 MB.
    with _trava:
        if _trab.encerrar():
            logger.info("Modelo OmniVoice e caches descarregados da VRAM.")
            return {"status": "unloaded"}
    return {"status": "not_loaded"}


@app.post("/load")
def load_model():
    t0 = time.time()
    r = _enfileirar_e_esperar({"tipo": "carregar"})
    if not r.get("ok"):
        raise HTTPException(status_code=500, detail=r.get("erro"))
    elapsed = time.time() - t0
    return {
        "status": "loaded",
        "elapsed_sec": round(elapsed, 2),
        "device": _trab.info.get("device", "CPU"),
        "vram_used_mb": float(_trab.info.get("vram_mb", 0.0))
    }


@app.get("/models")
def get_models_info():
    cache_path = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
    model_folder = os.path.join(cache_path, "hub", "models--k2-fsa--OmniVoice")
    if not os.path.exists(model_folder):
        model_folder = os.path.join(cache_path, "models--k2-fsa--OmniVoice")
    size_mb = 0.0
    if os.path.exists(model_folder):
        total_size = sum(os.path.getsize(os.path.join(dirpath, f)) for dirpath, _, filenames in os.walk(model_folder) for f in filenames)
        size_mb = round(total_size / (1024**2), 2)
    return {
        "models": [
            {
                "id": MODEL_NAME,
                "name": "OmniVoice 0.6B",
                "architecture": "Qwen3 DLM + Flow-Matching Diffusion",
                "loaded": _trab.vivo(),
                "local_path": model_folder if os.path.exists(model_folder) else None,
                "size_mb": size_mb,
                "precision": "float16",
                "modes": ["clone", "design", "auto"]
            }
        ]
    }


# =====================================================================================
# Rotas novas (OV-2): árbitro da GPU
# =====================================================================================
@app.get("/gpu/estado")
def gpu_estado():
    cfg = ler_config()
    vivo = _trab.vivo()
    parado_ha = time.time() - _estado["ultima_atividade"]
    minutos = cfg.get("inatividade_min", 10) or 0
    return {
        "dono": "omnivoice" if vivo else None,
        "modelo_carregado": vivo,
        "gerando": _estado["executando"] is not None,
        "aguardando": _estado["aguardando"],
        "na_fila": _fila.qsize(),
        "parado_ha_s": round(parado_ha, 1) if _estado["executando"] is None else 0.0,
        "descarrega_em_s": (round(max(0.0, minutos * 60 - parado_ha), 1)
                            if (vivo and minutos > 0 and _estado["executando"] is None) else None),
        "inatividade_min": minutos,
    }


class CederRequest(BaseModel):
    quem: Optional[str] = Field(default=None, description="quem está pedindo a placa (ex.: 'vozlab')")


@app.post("/gpu/ceder")
def gpu_ceder(req: Optional[CederRequest] = None):
    """Pedido de outro programa (o VozLab) para usar a placa. Nunca corta uma geração em andamento."""
    if _estado["executando"] is not None or not _fila.empty():
        return {"resultado": "ocupado", "gerando": _estado["executando"] is not None, "na_fila": _fila.qsize()}
    if not _trava.acquire(blocking=False):
        return {"resultado": "ocupado", "gerando": True, "na_fila": _fila.qsize()}
    try:
        if _trab.encerrar():
            logger.info(f"Placa cedida a pedido de {(req.quem if req else None) or 'outro programa'}.")
            return {"resultado": "cedido"}
        return {"resultado": "livre"}
    finally:
        _trava.release()


# =====================================================================================
# Rotas novas (OV-3): bloco com semente, cancelamento, informações
# =====================================================================================
class BlocoRequest(BaseModel):
    text: str = Field(..., min_length=1)
    language: Optional[str] = Field(default=None, description="Código do idioma ou null/'auto'")
    mode: str = Field(default="clone", description="'clone', 'design' ou 'auto'")
    ref_audio_path: Optional[str] = None
    ref_text: Optional[str] = None
    instruct: Optional[str] = None
    seed: Optional[int] = Field(default=None, description="No texto longo: base_seed + (idx - 1)")
    num_step: int = Field(default=16, ge=4, le=64)
    guidance_scale: float = Field(default=2.0, ge=0.0, le=4.0)
    speed: float = Field(default=1.0, ge=0.5, le=1.5)
    duration: Optional[float] = None
    denoise: bool = True
    preprocess_prompt: bool = True
    postprocess_output: bool = True
    t_shift: Optional[float] = None
    position_temperature: Optional[float] = None
    class_temperature: Optional[float] = None
    layer_penalty_factor: Optional[float] = None
    divisao_interna: bool = Field(default=False, description="False: o bloco sai inteiro (quem divide é o VozLab)")
    audio_chunk_duration: Optional[float] = None
    audio_chunk_threshold: Optional[float] = None
    pedido_id: Optional[str] = Field(default=None, description="Para cancelar este pedido em /v2/cancelar/{pedido_id}")


@app.post("/v2/gerar_bloco")
def v2_gerar_bloco(req: BlocoRequest):
    """Gera um bloco e devolve o WAV direto (float 32 bits). Passa pela mesma fila e pelo árbitro."""
    import io
    from fastapi.responses import Response
    pedido = req.model_dump()
    pedido["language"] = req.language if (req.language and req.language.lower() != "auto") else None
    pedido_id = req.pedido_id or f"bl_{uuid.uuid4().hex[:10]}"
    t0 = time.time()
    r = _enfileirar_e_esperar({"tipo": "bloco", "pedido_id": pedido_id, "pedido": pedido})
    if r.get("cancelado"):
        raise HTTPException(status_code=409, detail={"cancelado": True, "pedido_id": pedido_id})
    if not r.get("ok"):
        raise HTTPException(status_code=500, detail=r.get("erro"))
    buf = io.BytesIO()
    sf.write(buf, r["audio"], r["sampling_rate"], format="WAV", subtype="FLOAT")
    dur = len(r["audio"]) / r["sampling_rate"]
    return Response(content=buf.getvalue(), media_type="audio/wav", headers={
        "X-Pedido-Id": pedido_id,
        "X-Duracao-S": f"{dur:.3f}",
        "X-Segundos-Gerar": f"{r.get('segundos', 0):.2f}",
        "X-Segundos-Carga": f"{r.get('segundos_carga', 0):.2f}",
        "X-Segundos-Total": f"{time.time() - t0:.2f}",
        "X-Semente": "" if req.seed is None else str(req.seed),
        "X-Taxa": str(r["sampling_rate"]),
    })


@app.post("/v2/cancelar/{pedido_id}")
def v2_cancelar(pedido_id: str):
    return {"pedido_id": pedido_id, "situacao": cancelar_pedido(pedido_id)}


@app.get("/v2/info")
def v2_info():
    return {
        "versao_servico": "ov3",
        "recursos": ["fila_unica", "trabalhador_gpu", "arbitro_gpu", "semente", "cancelamento_real",
                     "bloco_sem_divisao_interna", "ajustes_nativos", "cache_voz_por_conteudo", "painel_em_/painel"],
        "ajustes_nativos_padrao": {"t_shift": 0.1, "position_temperature": 5.0, "class_temperature": 0.0,
                                    "layer_penalty_factor": 5.0, "audio_chunk_duration": 15.0,
                                    "audio_chunk_threshold": 30.0},
        "precisao": _trab.info.get("dtype") or "float16",
    }


# =====================================================================================
# Painel nativo (Gradio) dentro do serviço
# =====================================================================================
class _ModeloDoPainel:
    """O painel só precisa saber a taxa de amostragem; quem gera é o trabalhador, pela fila."""

    @property
    def sampling_rate(self):
        return _trab.info.get("sampling_rate", 24000)


def _gerar_pelo_painel(text, language, ref_audio, instruct, num_step, guidance_scale, denoise, speed, duration,
                       preprocess_prompt, postprocess_output, mode, ref_text=None):
    # Mesmas mensagens e o mesmo comportamento do painel original (omnivoice/cli/demo.py, _gen_core)
    if not text or not text.strip():
        return None, "Please enter the text to synthesize."
    if mode == "clone" and not ref_audio:
        return None, "Please upload a reference audio."
    pedido = {
        "mode": mode, "text": text, "language": language if (language and language != "Auto") else None,
        "ref_audio_path": ref_audio, "ref_text": ref_text, "instruct": instruct,
        "num_step": int(num_step or 32),
        "guidance_scale": float(guidance_scale) if guidance_scale is not None else 2.0,
        "denoise": bool(denoise) if denoise is not None else True,
        "speed": float(speed) if speed is not None else 1.0,
        "duration": float(duration) if duration is not None else None,
        "preprocess_prompt": bool(preprocess_prompt), "postprocess_output": bool(postprocess_output),
        "usar_cache": False,
    }
    r = _enfileirar_e_esperar({"tipo": "painel", "pedido": pedido})
    if not r.get("ok"):
        return None, f"Error: {r.get('erro')}"
    waveform = (r["audio"] * 32767).astype(np.int16)
    return (r["sampling_rate"], waveform), "Done."


def _gerar_pedido_do_painel(pedido: Dict[str, Any]) -> Dict[str, Any]:
    """Painel em PT-BR (OV-6): o pedido vai para a MESMA fila e o MESMO trabalhador do VozLab."""
    return _enfileirar_e_esperar({"tipo": "painel", "pedido": pedido})


def montar_painel():
    # o Gradio não consulta a internet ao abrir (versão/estatísticas): tudo local
    os.environ.setdefault("GRADIO_ANALYTICS_ENABLED", "False")
    import gradio as gr
    cfg = ler_config()
    try:
        from omnivoice.utils.lang_map import LANG_NAMES, lang_display_name
        from omnivoice_painel_ptbr import construir_painel
        nomes = sorted(lang_display_name(n) for n in LANG_NAMES)
        gr.mount_gradio_app(app, construir_painel(_gerar_pedido_do_painel, nomes), path="/painel")
        logger.info(f"Painel do OmniVoice (PT-BR) disponível em http://127.0.0.1:{PORTA}/painel")
    except Exception as e:
        # O serviço continua funcionando para o VozLab mesmo se o painel falhar.
        logger.error(f"Painel PT-BR não pôde ser montado: {e}", exc_info=True)
    if cfg.get("montar_painel_original", False):
        try:
            from omnivoice.cli.demo import build_demo
            demo = build_demo(_ModeloDoPainel(), MODEL_NAME, generate_fn=_gerar_pelo_painel)
            gr.mount_gradio_app(app, demo, path="/painel-original")
            logger.info(f"Painel original (inglês/chinês) em http://127.0.0.1:{PORTA}/painel-original")
        except Exception as e:
            logger.error(f"Painel original não pôde ser montado: {e}", exc_info=True)


def atalho_porta_antiga(porta: int):
    """A porta antiga do painel (7860) só leva ao /painel do serviço."""
    import socket
    import uvicorn
    from fastapi.responses import RedirectResponse
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", porta)) == 0:
            logger.warning(f"Porta {porta} já está em uso (painel antigo aberto?): atalho não criado.")
            return
    atalho = FastAPI()

    @atalho.get("/{caminho:path}")
    def _ir(caminho: str = ""):
        return RedirectResponse(url=f"http://127.0.0.1:{PORTA}/painel/")

    servidor = uvicorn.Server(uvicorn.Config(atalho, host="127.0.0.1", port=porta, log_level="warning"))
    threading.Thread(target=servidor.run, name="atalho_7860", daemon=True).start()
    logger.info(f"Atalho: http://127.0.0.1:{porta} leva ao painel em /painel")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="OmniVoice Microservice")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORTA)
    args, _ = parser.parse_known_args()
    PORTA = args.port
    cfg = ler_config()
    threading.Thread(target=_despachante, name="despachante", daemon=True).start()
    threading.Thread(target=_vigia_inatividade, name="vigia_inatividade", daemon=True).start()
    if cfg.get("montar_painel", True):
        # Precisa ser montado ANTES de o servidor subir: montado depois, a fila interna do Gradio nunca liga
        # e os pedidos do painel ficam parados (medido na OV-2). Custa ~8 s a mais para o serviço responder.
        montar_painel()
    if cfg.get("porta_painel_antiga"):
        atalho_porta_antiga(int(cfg["porta_painel_antiga"]))
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")

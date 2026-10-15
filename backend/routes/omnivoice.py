"""Adaptador HTTP do OmniVoice para o Voicebox.

Este módulo expõe rotas aditivas no Voicebox sob o prefixo `/omnivoice`,
comunicando-se com o microserviço isolado do OmniVoice em 127.0.0.1:17495.
Garante isolamento estrito de dependências, leitura não-destrutiva da biblioteca
de vozes e degradação graciosa caso o serviço lateral esteja offline.
"""

import asyncio
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import Response as FastAPIResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from .. import config
from ..database import ProfileSample as DBProfileSample, VoiceProfile as DBVoiceProfile, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/omnivoice", tags=["omnivoice"])

OMNIVOICE_SERVICE_URL = os.environ.get("OMNIVOICE_SERVICE_URL", "http://127.0.0.1:17495")
OMNIVOICE_DIR = Path(os.environ.get("OMNIVOICE_DIR", str(Path(__file__).resolve().parents[2] / "OmniVoice")))  # achado 5 da varredura
OMNIVOICE_LAUNCHER_PATH = OMNIVOICE_DIR / "iniciar_omnivoice_service.bat"
TIMEOUT_FAST_SEC = 2.0
TIMEOUT_LONG_SEC = 10.0


def _load_voice_aliases() -> Dict[str, str]:
    """Carrega apelidos amigáveis do catálogo sem alterar o banco de dados."""
    search_paths = [
        Path(config.get_data_dir()) / "voice-aliases.json",
        Path(__file__).resolve().parent.parent.parent / "frontend" / "novo" / "voice-aliases.json",
    ]
    for path in search_paths:
        if path.is_file():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data.get("aliases", {})
            except Exception as e:
                logger.debug(f"Falha ao ler aliases de {path}: {e}")
    return {}


async def _check_service_health() -> tuple[bool, Dict[str, Any]]:
    """Verifica se o microserviço OmniVoice está respondendo."""
    # Serviço desligado: responde na hora (no Windows, conectar numa porta fechada custa ~2,3 s)
    try:
        from ..services.gpu_arbitro import _servico_escutando

        if not _servico_escutando():
            return False, {}
    except Exception:
        pass
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_FAST_SEC) as client:
            res = await client.get(f"{OMNIVOICE_SERVICE_URL}/health")
            if res.status_code == 200:
                return True, res.json()
    except Exception:
        pass
    return False, {}


class OmniVoiceGenerateRequest(BaseModel):
    mode: str = Field(default="clone", description="'clone', 'design' ou 'auto'")
    text: str = Field(..., min_length=1, description="Texto a sintetizar")
    language: Optional[str] = Field(default="pt", description="Código do idioma (ex: 'pt', 'en') ou null para auto")
    voice_id: Optional[str] = Field(default=None, description="ID do perfil na biblioteca do Voicebox para clonagem")
    ref_audio_path: Optional[str] = Field(default=None, description="Caminho direto para arquivo WAV de referência")
    ref_text: Optional[str] = Field(default=None, description="Transcrição opcional do áudio de referência")
    instruct: Optional[str] = Field(default=None, description="Instruções de tom, estilo ou descrição para Voice Design")
    num_step: int = Field(default=16, ge=4, le=64, description="Passos de difusão (padrão: 16)")
    guidance_scale: float = Field(default=2.0, ge=0.0, le=4.0, description="Escala de orientação CFG")
    speed: float = Field(default=1.0, ge=0.5, le=1.5, description="Velocidade da fala")
    duration: Optional[float] = Field(default=None, description="Duração alvo em segundos")
    denoise: bool = Field(default=True, description="Redução de ruído no áudio final")
    preprocess_prompt: bool = Field(default=True)
    postprocess_output: bool = Field(default=True)


class OmniVoiceControlRequest(BaseModel):
    action: str = Field(..., description="'load', 'unload' ou 'start'")


@router.get("/status")
@router.get("/health")
async def get_omnivoice_status():
    """Retorna o estado do microserviço OmniVoice com degradação graciosa."""
    online, data = await _check_service_health()
    if online:
        return {
            "status": "online",
            "message": "Serviço OmniVoice operacional em hardware dedicado.",
            "device": data.get("device", "Desconhecido"),
            "gpu_available": data.get("gpu_available", False),
            "vram_used_mb": data.get("vram_used_mb", 0.0),
            "model_loaded": data.get("model_loaded", False),
            "version": data.get("version", "0.2.1"),
            "service_url": OMNIVOICE_SERVICE_URL,
        }
    return {
        "status": "offline",
        "message": f"Serviço OmniVoice não está em execução. Inicie o serviço em {OMNIVOICE_DIR}.",
        "service_url": OMNIVOICE_SERVICE_URL,
        "launcher_available": OMNIVOICE_LAUNCHER_PATH.is_file(),
    }


@router.get("/modes")
async def get_omnivoice_modes():
    """Lista os modos de síntese suportados e configurações recomendadas."""
    return {
        "modes": [
            {
                "id": "clone",
                "name": "Clonagem de Voz (Voice Clone)",
                "description": "Clona com alta fidelidade a voz selecionada da sua biblioteca do Voicebox ou um arquivo WAV de 3 a 10 segundos.",
                "requires_reference": True,
                "supports_instruct": True,
            },
            {
                "id": "design",
                "name": "Design de Voz (Voice Design)",
                "description": "Cria uma nova voz a partir de descrições textuais detalhadas (ex: 'Uma mulher brasileira idosa com voz calma e calorosa').",
                "requires_reference": False,
                "supports_instruct": True,
            },
            {
                "id": "auto",
                "name": "Voz Direta (Auto Voice)",
                "description": "Síntese automática direta com o perfil padrão e neutro do modelo.",
                "requires_reference": False,
                "supports_instruct": False,
            },
        ],
        "default_steps": 16,
        "default_guidance": 2.0,
        "supported_languages_count": 600,
        "recommended_step_range": {"min": 8, "max": 32, "default": 16},
    }


@router.get("/models")
async def get_omnivoice_models():
    """Retorna informações do modelo OmniVoice 0.6B."""
    online, _ = await _check_service_health()
    if online:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_FAST_SEC) as client:
                res = await client.get(f"{OMNIVOICE_SERVICE_URL}/models")
                if res.status_code == 200:
                    return res.json()
        except Exception:
            pass

    return {
        "models": [
            {
                "id": "k2-fsa/OmniVoice",
                "name": "OmniVoice 0.6B",
                "architecture": "Qwen3 DLM + Flow-Matching Diffusion",
                "loaded": False,
                "local_path": str(Path(os.environ.get("VOICEBOX_MODELS_DIR") or os.environ.get("HF_HUB_CACHE") or str(Path(__file__).resolve().parents[2] / "modelos")) / "models--k2-fsa--OmniVoice"),
                "size_mb": 3116.1,
                "precision": "float16",
                "modes": ["clone", "design", "auto"],
            }
        ]
    }


@router.get("/voices")
async def get_omnivoice_voices(db: Session = Depends(get_db)):
    """Lista as vozes disponíveis na biblioteca do Voicebox como candidatas para clonagem.

    Lê exclusivamente em modo somente leitura do banco de dados e dos caminhos originais
    dos arquivos de áudio, mantendo os apelidos e metadados já atribuídos.
    """
    aliases = _load_voice_aliases()
    data_dir = Path(config.get_data_dir())

    profiles = db.query(DBVoiceProfile).order_by(DBVoiceProfile.name.asc()).all()
    results = []

    # 1. Opção virtual para Voice Design (sem necessidade de áudio de referência)
    results.append({
        "id": "ov_design",
        "name": "✨ Novo Design de Voz (Por Descrição)",
        "alias": "[Design] Criação por Atributos",
        "language": "pt",
        "voice_type": "designed",
        "has_reference_audio": False,
        "sample_audio_path": None,
        "reference_text": None,
        "description": "Gere uma voz única descrevendo características vocais como idade, tom, sotaque e velocidade.",
    })

    # 2. Vozes reais da biblioteca do Voicebox
    for p in profiles:
        alias = aliases.get(p.id)
        display_name = alias if alias else p.name

        sample = db.query(DBProfileSample).filter_by(profile_id=p.id).first()
        ref_path = None
        has_ref = False

        if sample and sample.audio_path:
            full_path = (data_dir / sample.audio_path).resolve()
            if full_path.is_file():
                ref_path = str(full_path)
                has_ref = True

        results.append({
            "id": p.id,
            "name": display_name,
            "original_name": p.name,
            "alias": alias,
            "language": p.language or "pt",
            "voice_type": getattr(p, "voice_type", "cloned") or "cloned",
            "has_reference_audio": has_ref,
            "sample_audio_path": ref_path,
            "reference_text": sample.reference_text if sample else None,
            "description": p.description,
            "avatar_path": p.avatar_path,
        })

    return {
        "count": len(results),
        "voices": results,
    }


@router.post("/generate")
async def generate_audio(req: OmniVoiceGenerateRequest, db: Session = Depends(get_db)):
    """Encaminha requisição de geração para o microserviço OmniVoice."""
    online, _ = await _check_service_health()
    if not online:
        return {
            "status": "offline",
            "message": f"Serviço OmniVoice não está em execução. Inicie o serviço em {OMNIVOICE_DIR}.",
            "error": "Serviço OmniVoice offline",
        }

    payload: Dict[str, Any] = {
        "mode": req.mode,
        "text": req.text,
        "language": req.language if req.language else None,
        "instruct": req.instruct,
        "num_step": req.num_step,
        "guidance_scale": req.guidance_scale,
        "speed": req.speed,
        "duration": req.duration,
        "denoise": req.denoise,
        "preprocess_prompt": req.preprocess_prompt,
        "postprocess_output": req.postprocess_output,
    }

    # Se for modo de clonagem e um voice_id for fornecido
    if req.mode == "clone":
        if req.voice_id and req.voice_id != "ov_design":
            sample = db.query(DBProfileSample).filter_by(profile_id=req.voice_id).first()
            if sample and sample.audio_path:
                full_path = (Path(config.get_data_dir()) / sample.audio_path).resolve()
                if full_path.is_file():
                    payload["ref_audio_path"] = str(full_path)
                    payload["ref_text"] = sample.reference_text
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Arquivo de referência não encontrado no disco para a voz selecionada: {full_path}"
                    )
            else:
                raise HTTPException(
                    status_code=400,
                    detail=f"A voz selecionada ({req.voice_id}) não possui amostra de áudio para clonagem."
                )
        elif req.ref_audio_path:
            payload["ref_audio_path"] = req.ref_audio_path
            payload["ref_text"] = req.ref_text

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG_SEC) as client:
            res = await client.post(f"{OMNIVOICE_SERVICE_URL}/generate", json=payload)
            if res.status_code == 200:
                return res.json()
            return {
                "status": "error",
                "message": f"Erro retornado pelo OmniVoice: {res.text}",
                "detail": res.json() if res.headers.get("content-type") == "application/json" else res.text,
            }
    except Exception as e:
        logger.error(f"Falha ao enviar requisição para OmniVoice: {e}", exc_info=True)
        return {
            "status": "error",
            "message": f"Falha de comunicação com o serviço OmniVoice: {str(e)}",
        }


@router.get("/status/{job_id}")
async def get_job_status(job_id: str):
    """Consulta o progresso e estado de um job no microserviço OmniVoice."""
    online, _ = await _check_service_health()
    if not online:
        return {
            "job_id": job_id,
            "status": "offline",
            "message": r"Serviço OmniVoice offline.",
        }

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_FAST_SEC) as client:
            res = await client.get(f"{OMNIVOICE_SERVICE_URL}/status/{job_id}")
            if res.status_code == 200:
                data = res.json()
                if data.get("status") == "completed":
                    data["voicebox_audio_url"] = f"/omnivoice/audio/{job_id}"
                return data
            raise HTTPException(status_code=res.status_code, detail=res.text)
    except HTTPException:
        raise
    except Exception as e:
        return {"job_id": job_id, "status": "error", "error": str(e)}


@router.get("/audio/{job_id}")
async def get_job_audio(job_id: str):
    """Encaminha o áudio gerado pelo OmniVoice."""
    online, _ = await _check_service_health()
    if not online:
        raise HTTPException(status_code=503, detail="Serviço OmniVoice offline.")

    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_LONG_SEC) as client:
            res = await client.get(f"{OMNIVOICE_SERVICE_URL}/audio/{job_id}")
            if res.status_code == 200:
                return FastAPIResponse(
                    content=res.content,
                    media_type="audio/wav",
                    headers={"Content-Disposition": f'inline; filename="{job_id}.wav"'},
                )
            raise HTTPException(status_code=res.status_code, detail="Áudio não disponível.")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Erro ao recuperar áudio: {e}")


# GPU 3 (Missão GPU por famílias): aquecimento depois de "carregar". Uma frase curtíssima no modo "auto" (sem voz de
# referência) prepara a etapa de "virar som": a 1ª geração real caiu de ~8,6 s para ~4,5 s a mais que a 2ª
# (relatorios/gpu_familias/gpu3.md). Roda em segundo plano, pela fila do próprio serviço; se falhar, nada muda.
_aquecendo: Optional[asyncio.Task] = None


async def _aquecer_omnivoice() -> None:
    corpo = {"mode": "auto", "text": "Olá.", "language": "pt", "num_step": 16, "seed": 42}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{OMNIVOICE_SERVICE_URL}/v2/gerar_bloco", json=corpo)
        logger.info("OmniVoice aquecido (HTTP %s)", r.status_code)
    except Exception as e:  # noqa: BLE001
        logger.info("Aquecimento do OmniVoice não rodou: %s", e)


def _agendar_aquecimento() -> None:
    global _aquecendo
    if os.environ.get("VOZLAB_AQUECER_OMNIVOICE", "1").lower() in ("0", "false", "no"):
        return
    if _aquecendo is None or _aquecendo.done():
        _aquecendo = asyncio.create_task(_aquecer_omnivoice())


@router.post("/control")
async def control_service(req: OmniVoiceControlRequest):
    """Executa ações de controle: carregar ('load'), descarregar ('unload') ou iniciar ('start')."""
    action = req.action.lower()

    if action == "start":
        online, _ = await _check_service_health()
        if online:
            return {"status": "already_running", "message": "O serviço OmniVoice já está online."}

        if not OMNIVOICE_LAUNCHER_PATH.is_file():
            raise HTTPException(
                status_code=404,
                detail=f"Script de inicialização não encontrado: {OMNIVOICE_LAUNCHER_PATH}"
            )

        try:
            DETACHED = 0x00000008 | 0x08000000 if os.name == "nt" else 0
            # Sem console e sem saída, o serviço trava antes de abrir a porta: a saída vai
            # para um arquivo de registro. O ambiente Python do VozLab não é repassado ao
            # do OmniVoice (o .bat ativa o .venv dele).
            env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
            log_path = Path(config.get_data_dir()) / "logs" / "omnivoice_service.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "ab") as log:
                subprocess.Popen(
                    ["cmd.exe", "/c", str(OMNIVOICE_LAUNCHER_PATH)],
                    cwd=str(OMNIVOICE_LAUNCHER_PATH.parent),
                    creationflags=DETACHED,
                    close_fds=True,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            # Aguarda até 4 segundos para checar se subiu (sem travar o servidor:
            # asyncio.sleep libera o laço para progresso, SSE e outras rotas).
            for _ in range(8):
                await asyncio.sleep(0.5)
                is_up, _ = await _check_service_health()
                if is_up:
                    return {"status": "started", "message": "Serviço OmniVoice iniciado com sucesso."}

            return {"status": "starting", "message": "Inicialização do serviço disparada em segundo plano."}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha ao iniciar serviço: {e}")

    online, _ = await _check_service_health()
    if not online:
        return {
            "status": "offline",
            "message": f"Serviço OmniVoice offline. Inicie o serviço em {OMNIVOICE_DIR}.",
        }

    if action in ("load", "unload"):
        try:
            # carregar leva ~25 s (espera a placa e, na 1ª vez, o modelo pode estar sendo baixado)
            espera = 900.0 if action == "load" else TIMEOUT_LONG_SEC
            async with httpx.AsyncClient(timeout=espera) as client:
                res = await client.post(f"{OMNIVOICE_SERVICE_URL}/{action}")
                corpo = res.json()
            if action == "load" and res.status_code == 200:
                _agendar_aquecimento()
                if isinstance(corpo, dict):
                    corpo["aquecimento"] = "agendado"
            return corpo
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Falha na ação '{action}': {e}")

    raise HTTPException(status_code=400, detail=f"Ação desconhecida: '{action}'. Use 'load', 'unload' ou 'start'.")


# ---------------------------------------------------------------------------------------------
# Árbitro da GPU (Missão OmniVoice, OV-2): rotas novas, aditivas
# ---------------------------------------------------------------------------------------------
class GpuCederRequest(BaseModel):
    quem: Optional[str] = Field(default=None, description="Quem pede a placa (ex.: 'omnivoice')")


@router.post("/gpu/ceder")
async def gpu_ceder(req: Optional[GpuCederRequest] = None):
    """O serviço OmniVoice pede a placa antes de carregar o modelo. Nunca corta uma geração do VozLab."""
    from ..services import gpu_arbitro

    return await gpu_arbitro.ceder_placa(req.quem if req else None)


@router.get("/gpu/estado")
async def gpu_estado():
    """Quem está com a placa agora (para o aviso discreto na tela)."""
    from ..services import gpu_arbitro

    servico = None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_FAST_SEC) as client:
            r = await client.get(f"{OMNIVOICE_SERVICE_URL}/gpu/estado")
            if r.status_code == 200:
                servico = r.json()
    except Exception:
        servico = None
    return {
        "vozlab_gerando": gpu_arbitro.vozlab_ocupado(),
        "vozlab": gpu_arbitro.estado(),
        "omnivoice": servico,
    }


# ---------------------------------------------------------------------------------------------
# OV-4: ajustes do OmniVoice como modelo do VozLab (passos e CFG), recolhidos na tela Generate
# ---------------------------------------------------------------------------------------------
class OmniVoiceAjustes(BaseModel):
    num_step: int = Field(default=16, ge=4, le=64, description="Passos de difusão")
    guidance_scale: float = Field(default=2.0, ge=0.0, le=4.0, description="CFG (escala de orientação)")


@router.get("/ajustes")
async def get_ajustes():
    from ..backends.omnivoice_backend import AJUSTES_PADRAO, ler_ajustes

    return {**ler_ajustes(), "padrao": AJUSTES_PADRAO}


@router.post("/ajustes")
async def set_ajustes(req: OmniVoiceAjustes):
    from ..backends.omnivoice_backend import gravar_ajustes

    return gravar_ajustes(req.num_step, req.guidance_scale)


# ---------------------------------------------------------------------------------------------
# OV-6+7: painel OmniVoice completo (aba 🌍). Rotas novas, aditivas.
# ---------------------------------------------------------------------------------------------
from fastapi import File, UploadFile  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402


@router.get("/catalogo")
async def painel_catalogo():
    """Vozes do catálogo do OmniVoice (pasta catalogo_vozes do OmniVoice), só leitura."""
    from ..services import omnivoice_painel as op

    vozes = op.catalogo()
    return {"count": len(vozes), "vozes": vozes}


@router.get("/catalogo/audio/{nome}")
async def painel_catalogo_audio(nome: str):
    from ..services import omnivoice_painel as op

    p = op.caminho_catalogo(nome)
    if p is None:
        raise HTTPException(status_code=404, detail="Voz do catálogo não encontrada")
    return FileResponse(str(p))


@router.post("/referencia")
async def painel_referencia(arquivo: UploadFile = File(...)):
    """Áudio de referência enviado na hora: guardado em <dados>/cache (NÃO entra na biblioteca)."""
    from ..services import omnivoice_painel as op

    conteudo = await arquivo.read()
    if len(conteudo) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Arquivo grande demais (máximo 50 MB)")
    try:
        return await asyncio.to_thread(op.salvar_referencia, conteudo, arquivo.filename or "audio")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/referencia/{ref_id}/audio")
async def painel_referencia_audio(ref_id: str):
    from ..services import omnivoice_painel as op

    p = op.caminho_referencia_avulsa(ref_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Referência não encontrada")
    return FileResponse(str(p), media_type="audio/wav")


class PainelGerarRequest(BaseModel):
    modo: str = Field(default="clone", description="'clone', 'design' ou 'auto'")
    texto: str = Field(..., min_length=1, max_length=50000)
    idioma: Optional[str] = Field(default="pt")
    origem: Optional[str] = Field(default=None, description="'biblioteca', 'catalogo' ou 'arquivo' (modo clone)")
    voz_id: Optional[str] = None
    catalogo_nome: Optional[str] = None
    referencia_id: Optional[str] = None
    texto_referencia: Optional[str] = None
    instrucao: Optional[str] = None
    ajustes: Optional[Dict[str, Any]] = Field(default=None, description="Ajustes nativos (num_step, seed, t_shift...)")
    pausas: Optional[Dict[str, Any]] = Field(default=None, description="None = sem pausas do VozLab; {} = Padrão")


@router.post("/painel/gerar")
async def painel_gerar(req: PainelGerarRequest):
    from ..services import omnivoice_painel as op

    if req.modo == "clone" and not req.origem:
        raise HTTPException(status_code=400, detail="Escolha de onde vem a voz")
    return op.iniciar(req.model_dump())


@router.get("/painel/{job}")
async def painel_estado(job: str):
    from ..services import omnivoice_painel as op

    e = op.estado(job)
    if e is None:
        raise HTTPException(status_code=404, detail="Geração não encontrada")
    return e


@router.post("/painel/{job}/cancelar")
async def painel_cancelar(job: str):
    from ..services import omnivoice_painel as op

    return {"cancelado": await op.cancelar(job)}


@router.get("/painel/{job}/audio")
async def painel_audio(job: str, format: Optional[str] = None):
    from ..services import omnivoice_painel as op

    arq = op.arquivo(job)
    if not arq:
        raise HTTPException(status_code=404, detail="Áudio não disponível")
    if format and format.lower() == "mp3":  # "MP3 junto" (15/10/2026)
        from ..services.companion_export import wav_to_mp3

        mp3 = await asyncio.to_thread(wav_to_mp3, arq)
        if not mp3:
            raise HTTPException(status_code=500, detail="Não foi possível criar o MP3")
        return FileResponse(mp3, media_type="audio/mpeg", filename=f"omnivoice_{job}.mp3")
    return FileResponse(arq, media_type="audio/wav", filename=f"omnivoice_{job}.wav")

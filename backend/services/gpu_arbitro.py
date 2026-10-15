"""
Árbitro da placa de vídeo entre o VozLab e o serviço OmniVoice (Missão OmniVoice, Fase OV-2).

Regra: um dono por vez, e nunca cortar uma geração.
- R1/R2: antes de uma geração do VozLab começar, pede a placa ao serviço (POST 17495/gpu/ceder).
  Se o OmniVoice estiver gerando (ou com pedidos na fila), espera e tenta de novo a cada 2 s.
- R3/R4: o serviço pede a placa ao VozLab (POST /omnivoice/gpu/ceder). Se não houver geração do VozLab
  rodando, os modelos de voz dele são descarregados e a placa é cedida.
- R5: se o serviço estiver desligado ou não responder em 3 s, segue em frente. Nenhum depende do outro.

Assimetria proposital (evita que um espere o outro para sempre): uma geração do VozLab que está ESPERANDO
a placa não conta como "ocupada"; um pedido do OmniVoice esperando o VozLab conta como "ocupado".
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Callable, Optional
from urllib.parse import urlparse

import httpx
import psutil

logger = logging.getLogger("voicebox.gpu_arbitro")

OMNIVOICE_SERVICE_URL = os.environ.get("OMNIVOICE_SERVICE_URL", "http://127.0.0.1:17495")
_PORTA_SERVICO = urlparse(OMNIVOICE_SERVICE_URL).port or 17495
TIMEOUT_S = 3.0
INTERVALO_S = 2.0

# Estado visível na tela (aviso discreto) e para o próprio árbitro
_estado = {"aguardando_omnivoice": None, "aguardando_desde": None, "ultimo_cedido_em": None}


def estado() -> dict:
    return dict(_estado)


def _servico_escutando() -> bool:
    """Há algum programa escutando na porta do serviço? Consulta local, instantânea.
    (No Windows, tentar conectar numa porta fechada demora ~2,3 s: isso atrasaria toda geração do VozLab.)"""
    try:
        porta = _PORTA_SERVICO
        return any(c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == porta
                   for c in psutil.net_connections(kind="tcp"))
    except Exception:
        return True  # na dúvida, pergunta ao serviço (com o tempo limite normal)


async def _pedir_ao_servico() -> dict:
    if not _servico_escutando():  # < 1 ms
        return {"resultado": "desligado"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.post(f"{OMNIVOICE_SERVICE_URL}/gpu/ceder", json={"quem": "vozlab"})
            if r.status_code == 404:
                return {"resultado": "sem_arbitro"}  # serviço antigo, sem a rota nova
            return r.json()
    except Exception:
        return {"resultado": "desligado"}


async def pedir_placa(generation_id: str, desistir: Optional[Callable[[], bool]] = None) -> dict:
    """Espera o OmniVoice liberar a placa antes de uma geração do VozLab começar (R1, R2, R5).

    `desistir()` verdadeiro (ex.: a geração foi cancelada enquanto esperava) encerra a espera na hora.
    """
    t0 = time.time()
    avisou = False
    while True:
        res = await _pedir_ao_servico()
        if res.get("resultado") != "ocupado":
            if avisou:
                logger.info("OmniVoice liberou a placa para %s após %.0f s.", generation_id, time.time() - t0)
            _estado["aguardando_omnivoice"] = None
            _estado["aguardando_desde"] = None
            res["esperou_s"] = round(time.time() - t0, 1)
            return res
        if not avisou:
            logger.info("Geração %s aguardando o OmniVoice terminar para usar a placa.", generation_id)
            avisou = True
            _estado["aguardando_omnivoice"] = generation_id
            _estado["aguardando_desde"] = t0
        if desistir is not None and desistir():
            _estado["aguardando_omnivoice"] = None
            _estado["aguardando_desde"] = None
            return {"resultado": "desistiu", "esperou_s": round(time.time() - t0, 1)}
        await asyncio.sleep(INTERVALO_S)


# OV-4: motores que geram FORA do VozLab (no serviço OmniVoice). Uma geração com eles não usa a placa
# pelo VozLab: não pede a placa ao serviço (ele mesmo vai usá-la) e não conta como "VozLab ocupado".
MOTORES_REMOTOS = {"omnivoice"}
_motor_por_geracao: dict[str, Optional[str]] = {}


def geracao_remota(generation_id: str) -> bool:
    """A geração usa um motor remoto (OmniVoice)? Consulta o banco uma vez por geração."""
    if generation_id not in _motor_por_geracao:
        motor = None
        try:
            from ..database import session as sessao
            from ..database.models import Generation

            if sessao.SessionLocal is not None:
                db = sessao.SessionLocal()
                try:
                    g = db.query(Generation).filter_by(id=generation_id).first()
                    motor = getattr(g, "engine", None) if g else None
                finally:
                    db.close()
        except Exception as e:
            logger.debug("Árbitro: não consegui ler o motor de %s (%s)", generation_id, e)
        _motor_por_geracao[generation_id] = motor
        if len(_motor_por_geracao) > 5000:  # não cresce sem limite
            _motor_por_geracao.pop(next(iter(_motor_por_geracao)))
    return _motor_por_geracao[generation_id] in MOTORES_REMOTOS


def vozlab_ocupado() -> bool:
    """Há geração do VozLab usando a placa agora? (uma que só está esperando a placa não conta;
    uma com o OmniVoice também não, porque quem usa a placa é o serviço)"""
    from . import task_queue
    return any(not t.done() and not geracao_remota(gid)
               for gid, t in list(task_queue._running_generation_tasks.items()))


def descarregar_modelos_de_voz() -> list[str]:
    """Descarrega os modelos que o VozLab tiver na placa (voz, Whisper e o modelo de texto)."""
    from ..backends import _tts_backends, check_model_loaded, get_all_model_configs, unload_model_by_config

    sempre = {"qwen", "whisper", "qwen_llm"}  # serviços próprios; os demais só se já foram criados
    descarregados = []
    for cfg in get_all_model_configs():
        if cfg.engine not in sempre and cfg.engine not in _tts_backends:
            continue
        try:
            if check_model_loaded(cfg) and unload_model_by_config(cfg):
                descarregados.append(cfg.model_name)
        except Exception as e:
            logger.warning("Não consegui descarregar %s: %s", cfg.model_name, e)
    try:
        import gc
        import torch
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:
        pass
    return descarregados


async def ceder_placa(quem: Optional[str] = None) -> dict:
    """Pedido do OmniVoice (R3, R4): cede a placa se o VozLab não estiver gerando."""
    if vozlab_ocupado():
        return {"resultado": "ocupado", "gerando": True}
    descarregados = await asyncio.to_thread(descarregar_modelos_de_voz)
    _estado["ultimo_cedido_em"] = time.time()
    if descarregados:
        logger.info("Placa cedida a %s: descarregados %s.", quem or "outro programa", ", ".join(descarregados))
        return {"resultado": "cedido", "descarregados": descarregados}
    return {"resultado": "livre", "descarregados": []}

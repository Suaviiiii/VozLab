#!/usr/bin/env python3
"""
Trabalhador de GPU do OmniVoice (Missão OmniVoice, Fase OV-2).

Roda num processo filho do serviço (omnivoice_service.py). É o ÚNICO processo que carrega o modelo na placa.
- Liberar a placa = o serviço encerra este processo (a placa volta a 0 MB; o /unload antigo deixava ~0,9 GB presos).
- Recebe um pedido por vez pela conexão (Pipe) e responde com o áudio (numpy float32) ou o erro.

Mensagens (dict):
  {"tipo": "carregar"}                       -> {"ok": True, "segundos", "device", "vram_mb", "sampling_rate"}
  {"tipo": "gerar", "pedido": {...}}         -> {"ok": True, "audio", "sampling_rate", "segundos"} ou {"ok": False, "erro"}
  {"tipo": "vram"}                           -> {"ok": True, "vram_mb", "device"}
  {"tipo": "sair"}                           -> encerra
"""

import os
import time
import hashlib
import logging
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent
PROMPT_CACHE_DIR = BASE_DIR / "cache_prompts"
MODEL_NAME = "k2-fsa/OmniVoice"

logger = logging.getLogger("omnivoice_trabalhador")


def _cache_prompt(model, memoria: Dict[str, Any], ref_audio_path: str, ref_text: Optional[str]):
    """Cache da voz preparada, em memória e em disco (cache_prompts).

    OV-3: a chave principal é o CONTEÚDO do áudio + o texto (a mesma voz em outra pasta é reaproveitada).
    Os arquivos antigos (chave = caminho + data + texto) continuam valendo: são lidos se existirem.
    """
    from omnivoice.models.omnivoice import VoiceClonePrompt
    try:
        mtime = os.path.getmtime(ref_audio_path)
    except Exception:
        mtime = 0
    chave_antiga = hashlib.sha256(f"{ref_audio_path}_{mtime}_{ref_text or ''}".encode("utf-8")).hexdigest()[:16]
    h = hashlib.sha256()
    with open(ref_audio_path, "rb") as f:
        for bloco in iter(lambda: f.read(1 << 20), b""):
            h.update(bloco)
    h.update(b"|" + (ref_text or "").encode("utf-8"))
    chave = "c_" + h.hexdigest()[:24]
    if chave in memoria:
        logger.info(f"Reutilizando VoiceClonePrompt do cache em memória: {chave}")
        return memoria[chave]
    arquivo = PROMPT_CACHE_DIR / f"{chave}.pt"
    for candidato in (arquivo, PROMPT_CACHE_DIR / f"{chave_antiga}.pt"):
        if candidato.exists():
            try:
                logger.info(f"Carregando VoiceClonePrompt do cache em disco: {candidato.name}")
                prompt = VoiceClonePrompt.load(str(candidato))
                memoria[chave] = prompt
                return prompt
            except Exception as e:
                logger.warning(f"Falha ao carregar prompt em cache ({e}), reextraindo...")
    logger.info(f"Extraindo novo VoiceClonePrompt para {Path(ref_audio_path).name}...")
    t0 = time.time()
    prompt = model.create_voice_clone_prompt(ref_audio=ref_audio_path, ref_text=ref_text or None)
    logger.info(f"VoiceClonePrompt extraído e compilado em {time.time() - t0:.2f}s.")
    try:
        PROMPT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        prompt.save(str(arquivo))
    except Exception as e:
        logger.warning(f"Falha ao persistir prompt em disco: {e}")
    memoria[chave] = prompt
    return prompt


def _gerar(model, memoria, p: Dict[str, Any]):
    import numpy as np
    from omnivoice import OmniVoiceGenerationConfig

    cfg = OmniVoiceGenerationConfig(
        num_step=int(p.get("num_step", 16)),
        guidance_scale=float(p.get("guidance_scale", 2.0)),
        denoise=bool(p.get("denoise", True)),
        preprocess_prompt=bool(p.get("preprocess_prompt", True)),
        postprocess_output=bool(p.get("postprocess_output", True)),
    )
    # OV-3: ajustes nativos do modelo, só quando pedidos (sem eles, o padrão do modelo continua)
    for nome in ("t_shift", "position_temperature", "class_temperature", "layer_penalty_factor",
                 "audio_chunk_duration", "audio_chunk_threshold"):
        if p.get(nome) is not None:
            setattr(cfg, nome, float(p[nome]))
    if p.get("divisao_interna") is False:
        cfg.audio_chunk_threshold = 1e9  # quem divide o texto é o VozLab: o modelo gera o bloco inteiro
    kw: Dict[str, Any] = {"text": p["text"].strip(), "language": p.get("language"), "generation_config": cfg}
    if p.get("speed") is not None and float(p["speed"]) != 1.0:
        kw["speed"] = float(p["speed"])
    if p.get("duration") and float(p["duration"]) > 0:
        kw["duration"] = float(p["duration"])
    if p.get("mode") == "clone":
        ref = p.get("ref_audio_path")
        if not ref or not os.path.exists(ref):
            raise ValueError(f"Áudio de referência não encontrado: {ref}")
        if p.get("usar_cache", True):
            kw["voice_clone_prompt"] = _cache_prompt(model, memoria, ref, p.get("ref_text"))
        else:
            # o painel nativo sempre preparou a voz na hora, sem cache (arquivos enviados são temporários)
            kw["voice_clone_prompt"] = model.create_voice_clone_prompt(ref_audio=ref, ref_text=p.get("ref_text") or None)
    if p.get("instruct") and str(p["instruct"]).strip():
        kw["instruct"] = str(p["instruct"]).strip()
    if p.get("seed") is not None:
        import torch
        torch.manual_seed(int(p["seed"]))  # o sorteio do modelo (gumbel) usa o gerador do torch
    audio = model.generate(**kw)
    return np.asarray(audio[0], dtype=np.float32)


class Cancelado(Exception):
    pass


def principal(conexao, cancelar=None, nivel_log=logging.INFO):
    """Laço do processo filho. `cancelar` (multiprocessing.Event) para a geração entre dois passos."""
    os.environ["HSA_OVERRIDE_GFX_VERSION"] = "11.0.0"
    os.environ.setdefault("HIP_VISIBLE_DEVICES", "0")
    logging.basicConfig(level=nivel_log, format="%(asctime)s [%(levelname)s] [trabalhador] %(message)s")
    import torch

    model = None
    memoria: Dict[str, Any] = {}
    _passos: list = []  # horário de cada passo do laço (para medir onde o tempo vai)
    MB = 1024 ** 2

    def carregar():
        nonlocal model
        if model is None:
            from omnivoice import OmniVoice
            t0 = time.time()
            logger.info(f"Carregando OmniVoice na GPU ({MODEL_NAME})...")
            # OMNIVOICE_DTYPE=float32: só para medir a memória em float32 (missão das placas); o padrão é float16
            usar32 = os.environ.get("OMNIVOICE_DTYPE") == "float32" or not torch.cuda.is_available()
            dtype = torch.float32 if usar32 else torch.float16
            model = OmniVoice.from_pretrained(
                MODEL_NAME,
                device_map="cuda:0" if torch.cuda.is_available() else "cpu",
                dtype=dtype,
                load_asr=False,  # o Whisper só carrega se uma referência vier sem texto (o próprio modelo cuida)
            )
            if cancelar is not None:
                def _verificar():
                    _passos.append(time.time())
                    if cancelar.is_set():
                        logger.info("Sinal de cancelamento visto entre dois passos: parando.")
                        raise Cancelado("geração cancelada")
                model._verificar_cancelamento = _verificar
            logger.info(f"Modelo carregado em {time.time() - t0:.2f}s com sucesso ({dtype}).")
        return model

    def info():
        disp = torch.cuda.get_device_name(0) if torch.cuda.is_available() else "CPU"
        vram = (torch.cuda.memory_allocated() / MB) if torch.cuda.is_available() else 0.0
        return disp, round(vram, 1), torch.cuda.is_available()

    while True:
        try:
            msg = conexao.recv()
        except (EOFError, OSError):
            break
        tipo = msg.get("tipo")
        try:
            if tipo == "sair":
                break
            if tipo == "carregar":
                t0 = time.time()
                m = carregar()
                disp, vram, gpu = info()
                conexao.send({"ok": True, "segundos": round(time.time() - t0, 2), "device": disp, "vram_mb": vram,
                              "gpu_available": gpu, "sampling_rate": m.sampling_rate,
                              "dtype": str(next(m.parameters()).dtype)})
            elif tipo == "vram":
                disp, vram, gpu = info()
                conexao.send({"ok": True, "device": disp, "vram_mb": vram, "gpu_available": gpu,
                              "modelo_carregado": model is not None})
            elif tipo == "gerar":
                t0 = time.time()
                m = carregar()
                _passos.clear()
                try:
                    onda = _gerar(m, memoria, msg["pedido"])
                except Cancelado:
                    logger.info(f"Geração cancelada após {time.time() - t0:.2f}s.")
                    conexao.send({"ok": False, "cancelado": True, "erro": "cancelado",
                                  "segundos": round(time.time() - t0, 2)})
                    continue
                finally:
                    if cancelar is not None:
                        cancelar.clear()
                fim = time.time()
                if _passos:
                    logger.info(f"Geração: {len(_passos)} passos de {_passos[0] - t0:.2f}s a {_passos[-1] - t0:.2f}s; "
                                f"som pronto em {fim - t0:.2f}s (o que vem depois do último passo não pode ser "
                                f"cancelado entre passos)")
                conexao.send({"ok": True, "audio": onda, "sampling_rate": m.sampling_rate,
                              "segundos": round(fim - t0, 2),
                              "passos_s": [round(_passos[0] - t0, 2), round(_passos[-1] - t0, 2)] if _passos else None})
            else:
                conexao.send({"ok": False, "erro": f"mensagem desconhecida: {tipo}"})
        except Exception as e:
            logger.error(f"Erro no trabalhador ({tipo}): {e}", exc_info=True)
            try:
                conexao.send({"ok": False, "erro": str(e)})
            except Exception:
                break

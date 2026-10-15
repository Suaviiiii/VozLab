"""
Geração do painel OmniVoice do VozLab (Missão OmniVoice, OV-6+7).

O painel (aba 🌍) gera com QUALQUER origem de voz: biblioteca do VozLab, catálogo do OmniVoice (pasta OmniVoice\\
catalogo_vozes), um arquivo enviado na hora (fica em <dados>/cache, nunca na biblioteca), "criar voz" (Design) ou a
voz automática. Com as pausas ligadas e texto de 2+ blocos, o texto passa pelo divisor v2 (blocos de 220), cada
bloco é gerado no serviço com semente base + (idx - 1), e as emendas v2 juntam com as pausas escolhidas e -19 LUFS.
Sem pausas, o texto vai numa ida só (o modelo divide por dentro, como no painel original).

Os trabalhos ficam em memória (a lista "Sessão" do painel) e o áudio em <dados>/cache/omnivoice_painel/.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import numpy as np

logger = logging.getLogger("voicebox.omnivoice_painel")

CATALOGO_DIR = Path(os.environ.get("OMNIVOICE_DIR", str(Path(__file__).resolve().parents[2] / "OmniVoice"))) / "catalogo_vozes"  # achado 5 da varredura
EXTENSOES = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
_trabalhos: Dict[str, Dict[str, Any]] = {}
_tarefas: Dict[str, asyncio.Task] = {}


def _pasta(nome: str) -> Path:
    from .. import config

    p = Path(config.get_cache_dir()) / nome
    p.mkdir(parents=True, exist_ok=True)
    return p


# ------------------------------------------------------------------------------------------- vozes
def catalogo() -> list[dict]:
    if not CATALOGO_DIR.is_dir():
        return []
    vozes = []
    for f in sorted(CATALOGO_DIR.iterdir()):
        if f.suffix.lower() in EXTENSOES:
            txt = f.with_suffix(".txt")
            vozes.append({"nome": f.stem, "arquivo": f.name,
                          "texto_referencia": txt.read_text(encoding="utf-8").strip() if txt.exists() else ""})
    return vozes


def caminho_catalogo(nome: str) -> Optional[Path]:
    for f in CATALOGO_DIR.iterdir() if CATALOGO_DIR.is_dir() else []:
        if f.stem == nome and f.suffix.lower() in EXTENSOES:
            return f
    return None


def salvar_referencia(conteudo: bytes, nome_original: str) -> dict:
    """Guarda um áudio enviado na hora em <dados>/cache/omnivoice_referencias_avulsas/ (NÃO entra na biblioteca).
    Convertido para WAV; mesmo arquivo enviado de novo = mesmo id (resumo do conteúdo)."""
    import soundfile as sf

    h = hashlib.sha256(conteudo).hexdigest()[:20]
    destino = _pasta("omnivoice_referencias_avulsas") / f"avulsa_{h}.wav"
    if not destino.exists():
        try:
            onda, sr = sf.read(io.BytesIO(conteudo), dtype="float32", always_2d=False)
        except Exception as e:
            raise ValueError(f"Não consegui ler o áudio '{nome_original}' (use WAV, FLAC ou OGG): {e}") from e
        if onda.ndim > 1:
            onda = onda.mean(axis=1)
        sf.write(str(destino), onda, sr)
    info = sf.info(str(destino))
    return {"id": h, "nome_original": nome_original, "duracao_s": round(info.duration, 2)}


def caminho_referencia_avulsa(ref_id: str) -> Optional[Path]:
    if not ref_id or not ref_id.isalnum():
        return None
    p = _pasta("omnivoice_referencias_avulsas") / f"avulsa_{ref_id}.wav"
    return p if p.exists() else None


# ------------------------------------------------------------------------------------------- geração
def _resolver_voz(pedido: dict, db) -> tuple[Optional[str], Optional[str]]:
    """Devolve (caminho da referência, texto da referência) conforme a origem."""
    from ..backends.omnivoice_backend import OmniVoiceBackend
    from .. import config
    from ..database import ProfileSample as DBProfileSample

    origem = pedido.get("origem")
    ref_txt = (pedido.get("texto_referencia") or "").strip() or None
    if origem == "biblioteca":
        sample = db.query(DBProfileSample).filter_by(profile_id=pedido.get("voz_id")).first()
        if not sample or not sample.audio_path:
            raise ValueError("A voz escolhida da biblioteca não tem amostra de áudio.")
        caminho = config.resolve_storage_path(sample.audio_path)
        if caminho is None or not Path(caminho).exists():
            raise ValueError("O áudio da voz escolhida não foi encontrado no disco.")
        caminho, ref_txt = str(caminho), (ref_txt or sample.reference_text)
    elif origem == "catalogo":
        p = caminho_catalogo(pedido.get("catalogo_nome") or "")
        if p is None:
            raise ValueError("Voz do catálogo não encontrada.")
        caminho = str(p)
    elif origem == "arquivo":
        p = caminho_referencia_avulsa(pedido.get("referencia_id") or "")
        if p is None:
            raise ValueError("O arquivo enviado não foi encontrado; envie de novo.")
        caminho = str(p)
    else:
        raise ValueError("Escolha de onde vem a voz.")
    # referência longa: cópia de ~10 s (a original não muda); o texto da inteira não bate mais com ela
    curta = OmniVoiceBackend()._referencia_curta(caminho)
    if curta:
        return curta, None
    return caminho, ref_txt


async def _gerar_bloco(cliente: httpx.AsyncClient, url: str, corpo: dict, trab: dict) -> tuple[np.ndarray, int]:
    import soundfile as sf

    trab["pedido_atual"] = corpo["pedido_id"]
    r = await cliente.post(f"{url}/v2/gerar_bloco", json=corpo)
    trab["pedido_atual"] = None
    if r.status_code == 409:
        raise asyncio.CancelledError()
    if r.status_code != 200:
        raise RuntimeError(f"o serviço OmniVoice falhou ({r.status_code}): {r.text[:300]}")
    onda, sr = sf.read(io.BytesIO(r.content), dtype="float32")
    return (onda.mean(axis=1) if onda.ndim > 1 else onda), int(sr)


async def _executar(job: str, pedido: dict):
    import soundfile as sf

    from ..backends.omnivoice_backend import OMNIVOICE_SERVICE_URL, OmniVoiceBackend
    from ..database import get_db
    from ..utils.chunked_tts import split_text_into_chunks_v2
    from ..utils.texto_longo_defaults import load_texto_longo_config
    from .long_generation import emendas_v2_settings

    trab = _trabalhos[job]
    t0 = time.time()
    db = next(get_db())
    try:
        trab["situacao"] = "preparando"
        await OmniVoiceBackend().load_model()  # liga o serviço se estiver desligado (~15 s)
        modo = pedido.get("modo", "clone")
        base: Dict[str, Any] = {k: v for k, v in (pedido.get("ajustes") or {}).items() if v is not None}
        base.setdefault("num_step", 16)
        lang = pedido.get("idioma")
        base["language"] = None if (not lang or lang == "auto") else lang
        if modo == "clone":
            ref, ref_txt = _resolver_voz(pedido, db)
            base.update(mode="clone", ref_audio_path=ref, ref_text=ref_txt)
        else:
            base.update(mode="design" if (pedido.get("instrucao") or "").strip() else "auto")
        if (pedido.get("instrucao") or "").strip():
            base["instruct"] = pedido["instrucao"].strip()
        semente = base.pop("seed", None)

        texto = pedido["texto"].strip()
        cfg = load_texto_longo_config()
        pausas = pedido.get("pausas")  # None = sem pausas do VozLab (uma ida só); {} = Padrão; dict = Curtas/Longas
        blocos = (split_text_into_chunks_v2(texto, max_chars=220, min_chars=cfg.get("min_chunk_chars", 40),
                                            language=(base["language"] or "pt"), as_objects=True, config=cfg)
                  if pausas is not None else [])
        trab["blocos_total"] = max(1, len(blocos))
        trab["situacao"] = "gerando"
        async with httpx.AsyncClient(timeout=3600.0) as cliente:
            if len(blocos) >= 2:
                ondas, tipos, sr = [], [], 24000
                for i, b in enumerate(blocos, 1):
                    corpo = {**base, "text": b.text, "divisao_interna": False,
                             "seed": (semente + i - 1) if semente is not None else None,
                             "pedido_id": f"painel_{job}_{i}"}
                    onda, sr = await _gerar_bloco(cliente, OMNIVOICE_SERVICE_URL, corpo, trab)
                    ondas.append(onda)
                    tipos.append(b.boundary_type)
                    trab["blocos_prontos"] = i
                from ..utils.emendas_v2 import assemble_v2

                ajustes_emenda = emendas_v2_settings(cfg, pausas)
                audio, _rel = await asyncio.to_thread(assemble_v2, ondas, sr, tipos, nivelar=False, **ajustes_emenda)
            else:
                corpo = {**base, "text": texto, "seed": semente, "pedido_id": f"painel_{job}_1",
                         "divisao_interna": True}
                audio, sr = await _gerar_bloco(cliente, OMNIVOICE_SERVICE_URL, corpo, trab)
                trab["blocos_prontos"] = 1
                if pausas is not None:  # 1 bloco: nivela no mesmo -19 LUFS do texto longo
                    from ..utils.emendas_v2 import assemble_v2

                    audio, _rel = await asyncio.to_thread(assemble_v2, [audio], sr, ["frase"], nivelar=False,
                                                          **emendas_v2_settings(cfg, pausas))
        destino = _pasta("omnivoice_painel") / f"{job}.wav"
        await asyncio.to_thread(sf.write, str(destino), np.asarray(audio, dtype=np.float32), sr)
        trab.update(situacao="pronto", arquivo=str(destino), duracao_s=round(len(audio) / sr, 2),
                    segundos=round(time.time() - t0, 1))
        logger.info("Painel OmniVoice %s: %.1f s de áudio em %.1f s (%s bloco(s))", job, len(audio) / sr,
                    time.time() - t0, trab["blocos_total"])
    except asyncio.CancelledError:
        trab.update(situacao="cancelado", segundos=round(time.time() - t0, 1))
    except Exception as e:
        logger.error("Painel OmniVoice %s falhou: %s", job, e, exc_info=True)
        trab.update(situacao="falhou", erro=str(e), segundos=round(time.time() - t0, 1))
    finally:
        db.close()
        _tarefas.pop(job, None)


def iniciar(pedido: dict) -> dict:
    job = uuid.uuid4().hex[:12]
    _trabalhos[job] = {"id": job, "situacao": "na_fila", "criado_em": time.time(), "blocos_total": None,
                       "blocos_prontos": 0, "erro": None, "pedido_atual": None,
                       "texto": pedido["texto"][:200], "modo": pedido.get("modo")}
    _tarefas[job] = asyncio.get_running_loop().create_task(_executar(job, pedido))
    return {"id": job}


def estado(job: str) -> Optional[dict]:
    t = _trabalhos.get(job)
    if not t:
        return None
    return {k: v for k, v in t.items() if k != "arquivo"} | {"tem_audio": bool(t.get("arquivo"))}


def arquivo(job: str) -> Optional[str]:
    return (_trabalhos.get(job) or {}).get("arquivo")


async def cancelar(job: str) -> bool:
    from ..backends.omnivoice_backend import OMNIVOICE_SERVICE_URL

    t = _trabalhos.get(job)
    if not t or t["situacao"] not in ("na_fila", "preparando", "gerando"):
        return False
    pedido = t.get("pedido_atual")
    if pedido:
        try:
            async with httpx.AsyncClient(timeout=3.0) as c:
                await c.post(f"{OMNIVOICE_SERVICE_URL}/v2/cancelar/{pedido}")
        except Exception:
            pass
    tarefa = _tarefas.get(job)
    if tarefa:
        tarefa.cancel()
    t["situacao"] = "cancelado"
    return True

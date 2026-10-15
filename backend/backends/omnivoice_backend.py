"""
Motor OmniVoice do VozLab (Missão OmniVoice, Fase OV-4).

O VozLab NÃO importa o OmniVoice: este motor conversa por HTTP com o serviço OmniVoice (pasta OmniVoice, porta 17495),
que roda no ambiente Python dele. A geração passa pela rota /v2/gerar_bloco (com semente) e pela fila e pelo
árbitro da GPU do serviço (OV-2/OV-3).

- Se o serviço estiver desligado, o motor tenta ligá-lo (o mesmo .bat do botão "Iniciar Serviço") e espera até 60 s.
- Referências com mais de 12 s são cortadas numa CÓPIA de ~10 s (a voz da biblioteca nunca é alterada); como o texto
  de referência deixa de bater com o áudio cortado, a cópia vai sem texto e o serviço transcreve sozinho (Whisper).
- Passos de difusão e CFG vêm de <dados>/config/omnivoice.json (a tela grava; ajustes recolhidos na Generate).
- Cancelar a geração no VozLab também cancela no serviço (/v2/cancelar), e a fila dele fica livre em até ~3 s.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import List, Optional, Tuple

import httpx
import numpy as np

from .base import combine_voice_prompts as _combine_voice_prompts

logger = logging.getLogger(__name__)

OMNIVOICE_SERVICE_URL = os.environ.get("OMNIVOICE_SERVICE_URL", "http://127.0.0.1:17495")
# Pasta do OmniVoice: variável OMNIVOICE_DIR (padrão: pasta OmniVoice ao lado do projeto). Achado 5 da varredura (caminhos fixos).
OMNIVOICE_DIR = Path(os.environ.get("OMNIVOICE_DIR", str(Path(__file__).resolve().parents[2] / "OmniVoice")))
OMNIVOICE_LAUNCHER_PATH = OMNIVOICE_DIR / "iniciar_omnivoice_service.bat"
REFERENCIA_MAX_S = 12.0     # acima disto, corta uma cópia
REFERENCIA_CORTE_S = 10.0   # tamanho alvo da cópia
AJUSTES_PADRAO = {"num_step": 16, "guidance_scale": 2.0}
TIMEOUT_GERAR_S = 1800.0


def ler_ajustes() -> dict:
    """Ajustes do OmniVoice no VozLab (passos e CFG), gravados pela tela em <dados>/config/omnivoice.json."""
    from .. import config

    ajustes = dict(AJUSTES_PADRAO)
    try:
        arq = Path(config.get_data_dir()) / "config" / "omnivoice.json"
        if arq.is_file():
            dados = json.loads(arq.read_text(encoding="utf-8"))
            if isinstance(dados.get("num_step"), (int, float)):
                ajustes["num_step"] = int(min(64, max(4, dados["num_step"])))
            if isinstance(dados.get("guidance_scale"), (int, float)):
                ajustes["guidance_scale"] = float(min(4.0, max(0.0, dados["guidance_scale"])))
    except Exception as e:
        logger.warning("Ajustes do OmniVoice ilegíveis (%s); usando o padrão.", e)
    return ajustes


def gravar_ajustes(num_step: int, guidance_scale: float) -> dict:
    from .. import config

    ajustes = {"num_step": int(min(64, max(4, num_step))), "guidance_scale": float(min(4.0, max(0.0, guidance_scale)))}
    arq = Path(config.get_data_dir()) / "config" / "omnivoice.json"
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps(ajustes, ensure_ascii=False, indent=2), encoding="utf-8")
    return ajustes


class OmniVoiceBackend:
    """Motor 'omnivoice': gera pelo serviço OmniVoice (processo e Python separados)."""

    def __init__(self):
        self.model_size = "default"
        self._pronto = False  # serviço novo (OV-3) confirmado no ar

    # ------------------------------------------------------------------ serviço
    def _escutando(self) -> bool:
        try:
            from ..services.gpu_arbitro import _servico_escutando

            return _servico_escutando()
        except Exception:
            return True

    def _get_model_path(self, model_size: str = "default") -> str:
        return "k2-fsa/OmniVoice"  # o modelo fica com o serviço OmniVoice; o VozLab não baixa nem carrega

    def _is_model_cached(self, model_size: str = "default") -> bool:
        return True  # quem baixa e guarda o modelo é o serviço OmniVoice

    def is_loaded(self) -> bool:
        """'Carregado' = o serviço está no ar com o modelo na placa (a 1ª geração após liberar recarrega ~13 s)."""
        if not self._escutando():
            return False
        try:
            r = httpx.get(f"{OMNIVOICE_SERVICE_URL}/health", timeout=2.0)
            return bool(r.json().get("model_loaded"))
        except Exception:
            return False

    async def _ligar_servico(self) -> None:
        if not OMNIVOICE_LAUNCHER_PATH.is_file():
            raise RuntimeError(
                "O serviço OmniVoice está desligado e o atalho para ligá-lo não foi encontrado "
                f"({OMNIVOICE_LAUNCHER_PATH})."
            )
        from .. import config

        env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")}
        log_path = Path(config.get_data_dir()) / "logs" / "omnivoice_service.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Ligando o serviço OmniVoice para gerar...")
        with open(log_path, "ab") as log:
            subprocess.Popen(
                ["cmd.exe", "/c", str(OMNIVOICE_LAUNCHER_PATH)],
                cwd=str(OMNIVOICE_LAUNCHER_PATH.parent),
                creationflags=(0x00000008 | 0x08000000) if os.name == "nt" else 0,
                close_fds=True,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                env=env,
            )
        for _ in range(120):
            await asyncio.sleep(0.5)
            if self._escutando():
                try:
                    async with httpx.AsyncClient(timeout=2.0) as c:
                        if (await c.get(f"{OMNIVOICE_SERVICE_URL}/health")).status_code == 200:
                            return
                except Exception:
                    pass
        raise RuntimeError("O serviço OmniVoice não respondeu em 60 s. Veja logs/omnivoice_service.log.")

    async def load_model(self, model_size: str = "default") -> None:
        """Garante o serviço OmniVoice no ar (e que é a versão nova, com /v2). O modelo carrega na 1ª geração."""
        if not self._escutando():
            await self._ligar_servico()
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{OMNIVOICE_SERVICE_URL}/v2/info")
        except Exception as e:
            raise RuntimeError(f"O serviço OmniVoice não respondeu: {e}") from e
        if r.status_code != 200:
            raise RuntimeError(
                "O serviço OmniVoice no ar é a versão antiga (sem /v2). Desligue-o (DESLIGAR_OMNIVOICE_SERVICE.bat) "
                "e gere de novo."
            )
        self._pronto = True

    def unload_model(self) -> None:
        """Libera a placa no serviço (encerra o trabalhador dele: 0 MB). O serviço continua no ar."""
        if not self._escutando():
            return
        try:
            httpx.post(f"{OMNIVOICE_SERVICE_URL}/unload", timeout=120.0)
        except Exception as e:
            logger.warning("Não consegui descarregar o OmniVoice: %s", e)

    # ------------------------------------------------------------------ voz
    def _referencia_curta(self, audio_path: str) -> Optional[str]:
        """Se a referência passar de 12 s, cria (uma vez) uma cópia de ~10 s cortada num trecho baixo. A original
        não é tocada. Devolve o caminho da cópia, ou None se não precisar cortar."""
        import soundfile as sf

        from .. import config

        info = sf.info(audio_path)
        if info.duration <= REFERENCIA_MAX_S:
            return None
        h = hashlib.sha256(Path(audio_path).read_bytes()).hexdigest()[:20]
        destino = Path(config.get_cache_dir()) / "omnivoice_referencias" / f"ref10_{h}.wav"
        if destino.is_file():
            return str(destino)
        onda, sr = sf.read(audio_path, dtype="float32", always_2d=False)
        if onda.ndim > 1:
            onda = onda.mean(axis=1)
        # corta no trecho mais baixo (pausa) entre 8 e 10 s, em janelas de 20 ms
        ini, fim, jan = int(8.0 * sr), int(REFERENCIA_CORTE_S * sr), int(0.02 * sr)
        corte = fim
        if fim <= len(onda):
            energias = [(float(np.sqrt(np.mean(onda[i:i + jan] ** 2))), i) for i in range(ini, fim - jan, jan)]
            if energias:
                corte = min(energias)[1] + jan // 2
        onda = onda[:corte].copy()
        fade = min(int(0.03 * sr), len(onda))
        if fade:
            onda[-fade:] *= np.linspace(1.0, 0.0, fade, dtype=np.float32)
        destino.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(destino), onda, sr)
        logger.info("Referência de %.1f s cortada para %.1f s (cópia): %s", info.duration, corte / sr, destino.name)
        return str(destino)

    async def create_voice_prompt(
        self,
        audio_path: str,
        reference_text: str,
        use_cache: bool = True,
    ) -> Tuple[dict, bool]:
        """A voz é preparada no serviço (com o cache dele). Aqui só se escolhe o arquivo e o texto de referência."""
        curta = await asyncio.to_thread(self._referencia_curta, str(audio_path))
        if curta:
            # o texto da referência inteira não bate com o áudio cortado: o serviço transcreve a cópia sozinho
            return {"ref_audio": curta, "ref_text": None, "referencia_cortada": True}, False
        return {"ref_audio": str(audio_path), "ref_text": reference_text or None}, False

    async def combine_voice_prompts(
        self,
        audio_paths: List[str],
        reference_texts: List[str],
    ) -> Tuple[np.ndarray, str]:
        return await _combine_voice_prompts(audio_paths, reference_texts)

    # ------------------------------------------------------------------ gerar
    async def generate(
        self,
        text: str,
        voice_prompt: dict,
        language: str = "en",
        seed: Optional[int] = None,
        instruct: Optional[str] = None,
    ) -> Tuple[np.ndarray, int]:
        import soundfile as sf

        if not self._pronto:
            await self.load_model()
        ajustes = ler_ajustes()
        corpo = {
            "text": text,
            "language": language or None,
            "seed": seed,
            "num_step": ajustes["num_step"],
            "guidance_scale": ajustes["guidance_scale"],
            "divisao_interna": False,
            "pedido_id": f"vozlab_{uuid.uuid4().hex[:12]}",
        }
        if voice_prompt.get("voice_type") == "designed":
            corpo.update(mode="design", instruct=voice_prompt.get("design_prompt"))
        elif voice_prompt.get("ref_audio"):
            corpo.update(mode="clone", ref_audio_path=voice_prompt["ref_audio"], ref_text=voice_prompt.get("ref_text"))
        else:
            raise ValueError("O OmniVoice no VozLab precisa de uma voz clonada da biblioteca (com amostra de áudio).")
        if instruct and instruct.strip() and corpo["mode"] == "clone":
            corpo["instruct"] = instruct.strip()

        t0 = time.time()
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_GERAR_S) as c:
                r = await c.post(f"{OMNIVOICE_SERVICE_URL}/v2/gerar_bloco", json=corpo)
        except asyncio.CancelledError:
            # cancelado no VozLab: cancela também no serviço, para a fila dele ficar livre
            try:
                httpx.post(f"{OMNIVOICE_SERVICE_URL}/v2/cancelar/{corpo['pedido_id']}", timeout=3.0)
                logger.info("Geração OmniVoice %s cancelada também no serviço.", corpo["pedido_id"])
            except Exception:
                pass
            raise
        if r.status_code == 409:
            raise asyncio.CancelledError("Geração OmniVoice cancelada no serviço")
        if r.status_code != 200:
            raise RuntimeError(f"O serviço OmniVoice falhou ({r.status_code}): {r.text[:300]}")
        onda, sr = sf.read(io.BytesIO(r.content), dtype="float32")
        if onda.ndim > 1:
            onda = onda.mean(axis=1)
        logger.info(
            "[OmniVoice] %.2f s de áudio em %.1f s (carga %s s, semente %s, %s passos, CFG %s)",
            len(onda) / sr, time.time() - t0, r.headers.get("x-segundos-carga"), seed,
            ajustes["num_step"], ajustes["guidance_scale"],
        )
        return onda.astype(np.float32), int(sr)

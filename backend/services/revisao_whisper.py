"""
Revisão automática de blocos com o Whisper (Fase 7, "modo qualidade").

Fluxo, bloco a bloco, logo depois de a voz gerar cada bloco:
  gera -> o Whisper ouve -> checagens óbvias -> (opcional) refaz com semente nova,
  no máximo 2 vezes, e FICA A MELHOR versão (nunca troca por uma pior).
No fim do trabalho: pontos fora da curva do próprio trabalho são MARCADOS para o
usuário ouvir (mediana + 2 x desvio robusto, com teto alto). Nunca um limite fixo.

Tudo desligado por padrão. Três chaves, escolhidas na tela para cada trabalho:
  - revisar:          o Whisper ouve e marca suspeitos;
  - refazer_sozinho:  refaz os reprovados (só com "revisar" ligado);
  - voz_com_sotaque:  o sotaque engana o Whisper (testes da Fase 7 com uma voz francesa:
                      todos os julgamentos errados). Desliga a checagem de idioma e a
                      refação; só marca o que foge muito da curva ou perdeu fala.

O Whisper roda na MESMA placa do motor de voz (na CPU, com o PyTorch ROCm, levava
~190 s por bloco). Ele fica carregado só durante o trabalho e é descarregado no fim.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from math import gcd
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

PADRAO = {
    "modelo": "small",
    "max_tentativas_extra": 2,
    "salto_semente": 10000,      # tentativa k usa semente + k * salto
    "p_idioma_min": 0.30,
    "wer_idioma": 0.70,          # idioma "errado" só reprova junto com WER alto
    "cobertura_min": 0.60,
    "cobertura_max": 1.60,
    "wer_teto": 0.90,
    "multiplo_desvio": 2.0,      # escuta da Fase 7: 3x deixou passar um bloco que alucinou
    "desvio_minimo": 0.05,
}
JANELA_S = 30.0  # o Whisper só ouve 30 s por vez: blocos maiores são ouvidos em pedaços


def config_revisao(cfg: Optional[dict]) -> Dict[str, Any]:
    out = dict(PADRAO)
    out.update((cfg or {}).get("revisao_whisper") or {})
    return out


# ---------------------------------------------------------------------------
# Comparação texto x transcrição (sem modelo; testável)
# ---------------------------------------------------------------------------

def palavras(texto: str, idioma: str = "pt") -> List[str]:
    """Normaliza para comparar. Em português, números/moedas/unidades por extenso."""
    t = texto or ""
    if idioma == "pt":
        try:
            from ..utils.normalizador_wer import expand_numbers_pt

            t = expand_numbers_pt(t)
        except Exception:  # num2words ausente: compara sem expandir
            pass
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    return re.findall(r"\w+", t)


def wer(ref: List[str], hyp: List[str]) -> float:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            prev, d[j] = d[j], min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
    return d[-1] / max(1, len(ref))


def repeticao(seq: List[str], max_n: int = 4, vezes: int = 3) -> Optional[str]:
    """Trecho de 1 a 4 palavras que aparece `vezes` ou mais seguidas."""
    for n in range(1, max_n + 1):
        for i in range(0, len(seq) - n * vezes + 1):
            trecho = seq[i:i + n]
            if all(seq[i + k * n:i + (k + 1) * n] == trecho for k in range(1, vezes)):
                return " ".join(trecho)
    return None


def avaliar(texto: str, transcricao: str, idioma: str, p_idioma: Optional[float],
            sotaque: bool, par: Dict[str, Any]) -> Dict[str, Any]:
    ref, hyp = palavras(texto, idioma), palavras(transcricao, idioma)
    w = wer(ref, hyp)
    cobertura = len(hyp) / max(1, len(ref))
    rep = repeticao(hyp)
    if rep and repeticao(ref) == rep:
        rep = None
    motivos = []
    if not sotaque and p_idioma is not None and p_idioma < par["p_idioma_min"] and w > par["wer_idioma"]:
        motivos.append("idioma diferente do texto")
    if rep:
        motivos.append(f"repetição ('{rep}')")
    if cobertura < par["cobertura_min"]:
        motivos.append("faltou parte da fala")
    elif cobertura > par["cobertura_max"]:
        motivos.append("fala a mais que o texto")
    if w > par["wer_teto"]:
        motivos.append("muitas palavras diferentes do texto")
    if sotaque:
        # Com sotaque só a fala que sumiu é confiável como sinal "na hora".
        motivos = [m for m in motivos if m == "faltou parte da fala"]
    return {
        "wer": round(w, 3),
        "cobertura": round(cobertura, 2),
        "motivos": motivos,
        "aprovado": not motivos,
        "nota": round(len(motivos) * 10 + w, 3),  # menor é melhor
    }


def limite_fora_da_curva(wers: List[float], par: Dict[str, Any]) -> Optional[float]:
    if len(wers) < 3:
        return None
    v = np.asarray(wers, dtype=float)
    med = float(np.median(v))
    desvio = float(np.median(np.abs(v - med))) * 1.4826
    return min(par["wer_teto"], med + par["multiplo_desvio"] * max(desvio, par["desvio_minimo"]))


def marcar_para_ouvir(revisoes: Dict[int, Dict[str, Any]], par: Dict[str, Any]) -> Dict[str, Any]:
    """Recebe {idx: revisão} e devolve {idx: revisão com 'ouvir' e 'motivo_ouvir'} + resumo."""
    limite = limite_fora_da_curva([r["wer"] for r in revisoes.values()], par)
    marcados = []
    for idx, r in revisoes.items():
        motivos = list(r.get("motivos") or [])
        if limite is not None and r["wer"] > limite:
            motivos.append("fora da curva deste trabalho")
        r["ouvir"] = bool(motivos)
        r["motivo_ouvir"] = "; ".join(dict.fromkeys(motivos)) if motivos else None
        if r["ouvir"]:
            marcados.append(idx)
    return {"limite_fora_da_curva": None if limite is None else round(limite, 3), "marcados": sorted(marcados)}


# ---------------------------------------------------------------------------
# O revisor (carrega o Whisper sob demanda)
# ---------------------------------------------------------------------------

class RevisorBlocos:
    def __init__(self, idioma: str, escolha: Dict[str, Any], cfg: Optional[dict] = None):
        self.idioma = idioma or "pt"
        self.sotaque = bool(escolha.get("voz_com_sotaque"))
        self.refazer = bool(escolha.get("refazer_sozinho")) and not self.sotaque
        self.par = config_revisao(cfg)
        self._model = None
        self._proc = None

    # -- modelo -----------------------------------------------------------
    def _carregar(self):
        if self._model is not None:
            return
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        from ..backends import WHISPER_HF_REPOS
        from ..backends.base import get_torch_device

        repo = WHISPER_HF_REPOS.get(self.par["modelo"], f"openai/whisper-{self.par['modelo']}")
        self.device = get_torch_device()
        dtype = torch.float16 if self.device.startswith("cuda") else torch.float32
        self._dtype = dtype
        self._proc = WhisperProcessor.from_pretrained(repo)
        self._model = WhisperForConditionalGeneration.from_pretrained(repo, torch_dtype=dtype).to(self.device).eval()
        logger.info("Revisor Whisper %s carregado em %s", repo, self.device)

    def descarregar(self):
        if self._model is None:
            return
        from ..backends.base import empty_device_cache

        self._model = None
        self._proc = None
        import gc

        gc.collect()
        empty_device_cache(self.device)
        logger.info("Revisor Whisper descarregado")

    # -- ouvir --------------------------------------------------------------
    def ouvir(self, audio: np.ndarray, sr: int) -> Tuple[str, Optional[float]]:
        """Transcreve (em pedaços de 30 s) e mede a probabilidade do idioma do texto."""
        import torch
        from scipy.signal import resample_poly

        self._carregar()
        a = np.asarray(audio, dtype=np.float64)
        if a.ndim > 1:
            a = a.mean(axis=1)
        g = gcd(int(sr), 16000)
        a16 = resample_poly(a, 16000 // g, int(sr) // g).astype(np.float32)
        passo = int(JANELA_S * 16000)
        pedacos = [a16[i:i + passo] for i in range(0, max(1, len(a16)), passo)] or [a16]
        textos: List[str] = []
        p_idioma = None
        tok = self._proc.tokenizer
        for n, pedaco in enumerate(pedacos):
            if len(pedaco) < 1600 and n > 0:  # sobra menor que 0,1 s
                continue
            feats = self._proc(pedaco, sampling_rate=16000, return_tensors="pt").input_features
            feats = feats.to(self.device, self._dtype)
            with torch.inference_mode():
                if n == 0:
                    try:
                        from transformers.models.whisper.tokenization_whisper import LANGUAGES

                        codigos = list(LANGUAGES.keys())
                        ids = [tok.convert_tokens_to_ids(f"<|{c}|>") for c in codigos]
                        sot = tok.convert_tokens_to_ids("<|startoftranscript|>")
                        logits = self._model(input_features=feats,
                                             decoder_input_ids=torch.tensor([[sot]], device=self.device)).logits
                        p = torch.softmax(logits[0, -1, ids].float(), dim=-1).cpu().numpy()
                        if self.idioma in codigos:
                            p_idioma = float(p[codigos.index(self.idioma)])
                    except Exception as exc:  # idioma é só um sinal extra
                        logger.debug("Sem probabilidade de idioma: %s", exc)
                try:
                    out = self._model.generate(feats, language=self.idioma, task="transcribe")
                except Exception:
                    out = self._model.generate(feats, task="transcribe")
            textos.append(self._proc.batch_decode(out, skip_special_tokens=True)[0].strip())
        return " ".join(t for t in textos if t), p_idioma

    # -- um bloco -----------------------------------------------------------
    def revisar_bloco(
        self,
        texto: str,
        audio: np.ndarray,
        sr: int,
        semente: Optional[int],
        regerar: Callable[[Optional[int]], Tuple[np.ndarray, int]],
    ) -> Tuple[np.ndarray, int, Optional[int], Dict[str, Any]]:
        """Devolve (melhor áudio, sr, semente usada, revisão)."""
        tentativas = []
        atual = (audio, sr, semente)
        for k in range(self.par["max_tentativas_extra"] + 1):
            if k > 0:
                nova = None if semente is None else semente + k * int(self.par["salto_semente"])
                a, s = regerar(nova)
                atual = (a, s, nova)
            transcricao, p_idioma = self.ouvir(atual[0], atual[1])
            av = avaliar(texto, transcricao, self.idioma, p_idioma, self.sotaque, self.par)
            tentativas.append({**av, "semente": atual[2], "transcricao": transcricao[:400],
                               "p_idioma": None if p_idioma is None else round(p_idioma, 3),
                               "_audio": atual})
            if av["aprovado"] or not self.refazer:
                break
        melhor_k = min(range(len(tentativas)), key=lambda i: tentativas[i]["nota"])
        melhor = tentativas[melhor_k]
        a, s, sem = melhor.pop("_audio")
        for t in tentativas:
            t.pop("_audio", None)
        revisao = {
            "wer": melhor["wer"],
            "cobertura": melhor["cobertura"],
            "motivos": melhor["motivos"],
            "aprovado": melhor["aprovado"],
            "transcricao": melhor["transcricao"],
            "p_idioma": melhor["p_idioma"],
            "tentativas": [{"semente": t["semente"], "wer": t["wer"], "motivos": t["motivos"]} for t in tentativas],
            "escolhida": melhor_k,
            "refeito": len(tentativas) > 1,
            "voz_com_sotaque": self.sotaque,
        }
        return a, s, sem, revisao

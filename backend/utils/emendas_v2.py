"""
Emenda v2 e volume por bloco (Fase 4 da Missão Texto Longo).

Monta o áudio final de um trabalho com 2+ blocos:
1. apara o silêncio das pontas de cada bloco, com margem de segurança
   (nunca corta fala: só remove o que está abaixo do limiar);
2. iguala o volume percebido dos blocos (LUFS; RMS em blocos curtos), com
   ganho máximo limitado;
3. insere pausa controlada pelo tipo de fronteira (oração/frase/parágrafo),
   descontando o silêncio residual que a margem deixou;
4. fades curtos nas bordas, sem sobrepor fala sobre fala;
5. ajusta o master para o LUFS alvo, respeitando o pico real máximo (dBTP).

Só é usada com a flag ``emendas_v2`` ligada. O juntador antigo
(``concatenate_audio_chunks``) continua intacto e é a opção padrão.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Padrões escolhidos pelo usuário na escuta da Fase 4.
DEFAULT_PAUSAS_MS = {"oracao": 140, "frase": 300, "paragrafo": 775}
DEFAULT_TARGET_LUFS = -19.0
ENVELOPE_WINDOW_MS = 10.0


def _db(x: float) -> float:
    return 20.0 * np.log10(max(x, 1e-12))


def envelope_db(audio: np.ndarray, sr: int, window_ms: float = ENVELOPE_WINDOW_MS) -> np.ndarray:
    """RMS móvel centrado (em dBFS), uma posição por amostra."""
    n = max(1, int(sr * window_ms / 1000))
    power = np.convolve(audio.astype(np.float64) ** 2, np.ones(n) / n, mode="same")
    return 10.0 * np.log10(np.maximum(power, 1e-12))


def find_speech_bounds(audio: np.ndarray, sr: int, threshold_db: float) -> Optional[Tuple[int, int]]:
    """(primeira, última+1) amostra acima do limiar; None se o bloco é todo silêncio."""
    if len(audio) == 0:
        return None
    loud = np.flatnonzero(envelope_db(audio, sr) > threshold_db)
    if len(loud) == 0:
        return None
    return int(loud[0]), int(loud[-1]) + 1


def trim_edges(
    audio: np.ndarray, sr: int, threshold_db: float = -45.0, margin_ms: float = 40.0
) -> Tuple[np.ndarray, Dict[str, float]]:
    """Apara o silêncio das pontas mantendo ``margin_ms`` antes e depois da fala.

    Idempotente na prática: aparar de novo não remove mais nada, porque a
    margem que sobrou já está dentro do limite.
    """
    bounds = find_speech_bounds(audio, sr, threshold_db)
    if bounds is None:
        return audio.astype(np.float32, copy=True), {
            "trim_inicio_s": 0.0, "trim_fim_s": 0.0, "resid_inicio_s": 0.0, "resid_fim_s": 0.0,
            "silencioso": True,
        }
    first, last = bounds
    margin = int(sr * margin_ms / 1000)
    start = max(0, first - margin)
    end = min(len(audio), last + margin)
    out = audio[start:end].astype(np.float32, copy=True)
    return out, {
        "trim_inicio_s": start / sr,
        "trim_fim_s": (len(audio) - end) / sr,
        "resid_inicio_s": (first - start) / sr,
        "resid_fim_s": (end - last) / sr,
        "silencioso": False,
    }


def apply_edge_fades(audio: np.ndarray, sr: int, fade_ms: float) -> np.ndarray:
    n = min(int(sr * fade_ms / 1000), len(audio) // 2)
    if n <= 0:
        return audio
    out = audio.copy()
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, dtype=np.float64))
    out[:n] *= ramp.astype(np.float32)
    out[-n:] *= ramp[::-1].astype(np.float32)
    return out


def measure_loudness(audio: np.ndarray, sr: int, short_s: float = 0.4) -> Tuple[float, str]:
    """LUFS integrado (pyloudnorm); RMS em dBFS para blocos curtos."""
    if len(audio) / sr >= short_s:
        import pyloudnorm as pyln

        value = float(pyln.Meter(sr).integrated_loudness(audio.astype(np.float64)))
        if np.isfinite(value):
            return value, "lufs"
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2))) if len(audio) else 0.0
    return _db(rms), "rms"


def true_peak_dbtp(audio: np.ndarray, oversample: int = 4) -> float:
    from scipy.signal import resample_poly

    if len(audio) == 0:
        return -120.0
    up = resample_poly(audio.astype(np.float64), oversample, 1)
    return _db(float(np.max(np.abs(up))))


def lookahead_limiter(
    audio: np.ndarray, sr: int, ceiling_db: float = -1.5, hold_ms: float = 30.0, smooth_ms: float = 10.0
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Limitador de pico com antecipação: só atua perto de onde |x| passa do teto.

    1. ganho necessário por amostra (1 onde não há pico);
    2. mínimo numa janela de +-hold_ms (antecipa e segura em volta do pico);
    3. média móvel de smooth_ms (sem degraus, sem estalo).
    Como smooth_ms/2 <= hold_ms, a média só usa valores <= ao ganho necessário
    do pico: o teto é garantido em todas as amostras.
    """
    from scipy.ndimage import minimum_filter1d, uniform_filter1d

    ceiling = 10 ** (ceiling_db / 20.0)
    x = audio.astype(np.float64)
    need = np.minimum(1.0, ceiling / np.maximum(np.abs(x), 1e-12))
    if need.min() >= 1.0:
        return audio, {"amostras_afetadas_pct": 0.0, "reducao_max_db": 0.0}
    hold = minimum_filter1d(need, size=2 * max(1, int(sr * hold_ms / 1000)) + 1, mode="nearest")
    gain = uniform_filter1d(hold, size=max(1, int(sr * smooth_ms / 1000)), mode="nearest")
    gain = np.minimum(gain, need)  # redundante pela construção; trava de segurança
    return (x * gain).astype(np.float32), {
        "amostras_afetadas_pct": round(100.0 * float((gain < 0.999).mean()), 3),
        "reducao_max_db": round(-_db(float(gain.min())), 2),
    }


def nivelador_suave(
    audio: np.ndarray,
    sr: int,
    *,
    janela_ms: float = 400.0,
    passo_ms: float = 100.0,
    suavizar_ms: float = 800.0,
    porcao: float = 0.5,
    max_subida_db: float = 0.0,
    max_descida_db: float = 4.0,
    porta_db: float = -70.0,
    porta_relativa_db: float = 18.0,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Nivelador de volume (Fase 7): compressor suave DENTRO do áudio montado.
    Os parâmetros de subida continuam disponíveis para testes, mas o padrão nunca sobe nada.

    A emenda v2 só iguala o volume ENTRE blocos; na Fase 6 o usuário ouviu a voz cair
    no fim de algumas frases (janelas de 3 s até 4–7 LU abaixo da mediana).
    1. mede o RMS em janelas de 400 ms (passo de 100 ms);
    2. "fala" = janela a até 18 dB do nível típico da PRÓPRIA voz (porta relativa; a voz
       crua do Qwen sai perto de -40 dBFS, então uma porta fixa confundia chiado com fala);
    3. ganho de cada janela de fala = metade da distância até a mediana da fala (2:1),
       com limite de 0 dB / -4 dB: SÓ ABAIXA os trechos altos. Depois o LUFS final sobe tudo
       por igual. Levantar os trechos baixos (+6 dB, 1ª versão) levantava junto o chiado da voz;
       medida em 6 blocos da PTBR68: fala-chiado 22,6 dB sem nivelador, 22,0 subindo, 22,4 só
       abaixando; variação de volume (janelas de 3 s, faixa 10–90%) 3,2 / 2,0 / 2,3 LU;
    4. fora da fala (respiração, fim de frase quase mudo, chiado) o ganho NUNCA sobe:
       só pode descer. 1ª versão interpolava e levantava o chiado (escuta do usuário, Fase 7);
    5. média móvel de 800 ms: sem bombeamento audível, sem degraus.
    """
    from scipy.ndimage import uniform_filter1d

    x = audio.astype(np.float64)
    n = len(x)
    win = max(1, int(sr * janela_ms / 1000))
    hop = max(1, int(sr * passo_ms / 1000))
    if n < win * 2:
        return audio, {"aplicado": False}
    centros = np.arange(win // 2, n - win // 2, hop)
    sq = np.concatenate(([0.0], np.cumsum(x * x)))
    ini = centros - win // 2
    rms = np.sqrt(np.maximum((sq[ini + win] - sq[ini]) / win, 1e-20))
    nivel = 20 * np.log10(rms)
    audivel = nivel > porta_db
    if audivel.sum() < 3:
        return audio, {"aplicado": False}
    tipico = float(np.percentile(nivel[audivel], 75))
    fala = audivel & (nivel > tipico - porta_relativa_db)
    if fala.sum() < 3:
        return audio, {"aplicado": False}
    ref = float(np.median(nivel[fala]))
    ganho = np.clip((ref - nivel) * porcao, -max_descida_db, max_subida_db)
    ganho_janelas = np.interp(centros, centros[fala], ganho[fala])
    ganho_janelas = np.where(fala, ganho_janelas, np.minimum(ganho_janelas, 0.0))
    ganho_amostras = np.interp(np.arange(n), centros, ganho_janelas)
    ganho_amostras = uniform_filter1d(ganho_amostras, size=max(1, int(sr * suavizar_ms / 1000)), mode="nearest")
    out = (x * 10 ** (ganho_amostras / 20.0)).astype(np.float32)
    return out, {
        "aplicado": True,
        "referencia_db": round(ref, 2),
        "subida_max_db": round(float(ganho_amostras.max()), 2),
        "descida_max_db": round(float(-ganho_amostras.min()), 2),
    }


def assemble_v2(
    chunks: Sequence[np.ndarray],
    sr: int,
    boundary_types: Sequence[str],
    *,
    pausas_ms: Optional[Dict[str, float]] = None,
    fade_ms: float = 15.0,
    trim_threshold_db: float = -45.0,
    trim_margin_ms: float = 40.0,
    target_lufs: Optional[float] = DEFAULT_TARGET_LUFS,
    max_gain_db: float = 8.0,
    max_dbtp: float = -1.0,
    short_s: float = 0.4,
    equalize: bool = True,
    nivelar: bool = False,
) -> Tuple[np.ndarray, Dict[str, Any]]:
    """Monta os blocos com a emenda v2. ``boundary_types[i]`` é a fronteira
    DEPOIS do bloco i (o último valor é ignorado).

    Devolve (áudio float32, relatório com posições, pausas e ganhos).
    """
    pausas = dict(DEFAULT_PAUSAS_MS)
    if pausas_ms:
        pausas.update(pausas_ms)

    # 1ª passada: apara com o limiar absoluto só para MEDIR o volume da fala.
    raw = [np.asarray(c, dtype=np.float32) for c in chunks]
    pre = [trim_edges(c, sr, trim_threshold_db, trim_margin_ms) for c in raw]
    trimmed: List[np.ndarray] = [t for t, _ in pre]
    trims: List[Dict[str, float]] = [info for _, info in pre]

    # Volume por bloco: mede, calibra o RMS dos curtos contra os longos, iguala à mediana.
    loud = [measure_loudness(t, sr, short_s) if not tr["silencioso"] else (None, "silencio")
            for t, tr in zip(trimmed, trims)]
    gains_db = [0.0] * len(trimmed)
    reference = None
    if equalize:
        lufs_vals = [v for v, kind in loud if kind == "lufs"]
        rms_offset = 0.0
        pairs = [(v, measure_loudness(t, sr, short_s=1e9)[0]) for t, (v, k) in zip(trimmed, loud) if k == "lufs"]
        if pairs:
            rms_offset = float(np.median([lu - rm for lu, rm in pairs]))
        est = [None if v is None else (v if k == "lufs" else v + rms_offset) for v, k in loud]
        valid = [e for e in est if e is not None]
        if valid and lufs_vals:
            reference = float(np.median(valid))
            gains_db = [0.0 if e is None else float(np.clip(reference - e, -max_gain_db, max_gain_db)) for e in est]

    # 2ª passada: o "silêncio" é julgado no nível em que o bloco vai SAIR
    # (ganho do bloco + ganho estimado do master). Sem isso, um bloco baixo
    # que sobe 10 dB teria a respiração da margem audível e a pausa encurtaria.
    master_est = (target_lufs - reference) if (target_lufs is not None and reference is not None) else 0.0
    thresholds = []
    for i, c in enumerate(raw):
        thr = float(np.clip(trim_threshold_db - (gains_db[i] + master_est), -65.0, -30.0))
        thresholds.append(round(thr, 1))
        trimmed[i], trims[i] = trim_edges(c, sr, thr, trim_margin_ms)

    pieces: List[np.ndarray] = []
    junctions: List[Dict[str, Any]] = []
    positions: List[Tuple[int, int]] = []
    pos = 0
    for i, t in enumerate(trimmed):
        block = apply_edge_fades(t * np.float32(10 ** (gains_db[i] / 20.0)), sr, fade_ms)
        pieces.append(block)
        block_start = pos
        pos += len(block)
        positions.append((block_start, pos))
        if i < len(trimmed) - 1:
            btype = boundary_types[i] if i < len(boundary_types) else "frase"
            target_s = pausas.get(btype, pausas["frase"]) / 1000.0
            resid = trims[i]["resid_fim_s"] + trims[i + 1]["resid_inicio_s"]
            insert_s = max(0.0, target_s - resid)
            gap = np.zeros(int(round(insert_s * sr)), dtype=np.float32)
            pieces.append(gap)
            junctions.append({
                "apos_bloco": i + 1,
                "fronteira": btype,
                "pausa_alvo_s": target_s,
                "silencio_residual_s": round(resid, 4),
                "pausa_inserida_s": round(len(gap) / sr, 4),
                "inicio_bloco_amostra": block_start,
                "fim_bloco_amostra": pos,
            })
            pos += len(gap)

    audio = np.concatenate(pieces) if pieces else np.zeros(0, dtype=np.float32)

    nivelador_info: Dict[str, Any] = {"aplicado": False}
    if nivelar and len(audio):
        # Antes do LUFS final: o ganho do master e o limitador continuam valendo depois.
        audio, nivelador_info = nivelador_suave(audio, sr)

    master_gain_db = 0.0
    lufs_antes = None
    limiter_info: Dict[str, Any] = {}
    if target_lufs is not None and len(audio):
        lufs_antes, _ = measure_loudness(audio, sr, short_s)
        master_gain_db = target_lufs - lufs_antes
        audio = (audio * np.float32(10 ** (master_gain_db / 20.0))).astype(np.float32)
        # Teto de amostra 0,5 dB abaixo do dBTP alvo (folga para picos entre amostras).
        audio, limiter_info = lookahead_limiter(audio, sr, ceiling_db=max_dbtp - 0.5)
        tp = true_peak_dbtp(audio)
        if tp > max_dbtp:
            corr = tp - max_dbtp
            audio = (audio * np.float32(10 ** (-corr / 20.0))).astype(np.float32)
            master_gain_db -= corr
            limiter_info["correcao_final_db"] = round(corr, 2)

    report = {
        "blocos": len(chunks),
        "referencia_lufs_blocos": reference,
        "ganhos_db": [round(g, 2) for g in gains_db],
        "medida_bloco": [k for _, k in loud],
        "trims": trims,
        "limiar_corte_dbfs": thresholds,
        "emendas": junctions,
        "posicoes_blocos": positions,
        "master_ganho_db": round(master_gain_db, 2),
        "master_lufs_antes": lufs_antes,
        "limitador": limiter_info,
        "nivelador": nivelador_info,
    }
    return audio, report

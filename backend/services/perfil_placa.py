"""
Perfil da placa de vídeo (Missão GPU por famílias, fase GPU 2).

Descobre a FAMÍLIA da placa (pelo nome e pela arquitetura) e a classe de VRAM (memória da placa de vídeo: 4, 6 ou
8 GB) e diz, para cada modelo, se ele cabe. Os números vêm das medições da GPU 1
(`relatorios/gpu_familias/gpu1_medicoes.md`, seção 5) e das decisões do usuário (15/10/2026):
- GTX 1000 (4 GB): só o OmniVoice em float16, "não testado"; os outros: "não utilizável nesta placa";
- placas de 6 GB: os modelos 1.7B aparecem COM AVISO (podem ficar lentos ou falhar em blocos longos);
- sem placa compatível: aviso de que o VozLab precisa de uma placa de vídeo (não há modo CPU suportado).

Só INFORMA: nada aqui bloqueia uma geração nem muda a precisão dos modelos.
"""

from __future__ import annotations

import re
from typing import Optional

# Situação de cada modelo num perfil
OK = "ok"
AVISO = "aviso"
NAO_TESTADO = "nao_testado"
NAO_UTILIZAVEL = "nao_utilizavel"

# Famílias do escopo (decisão de 15/10/2026). As outras são reconhecidas, mas "fora do escopo".
FAMILIAS = {
    "amd_rx7000": {"nome": "AMD Radeon RX 7000", "no_escopo": True, "testada": True},
    "amd_rx6000": {"nome": "AMD Radeon RX 6000", "no_escopo": True, "testada": False},
    "nvidia_rtx3000": {"nome": "NVIDIA GeForce RTX 3000", "no_escopo": True, "testada": False},
    "nvidia_gtx1000": {"nome": "NVIDIA GeForce GTX 1000", "no_escopo": True, "testada": False},
    "amd_rx9000": {"nome": "AMD Radeon RX 9000", "no_escopo": False, "testada": False},
    "nvidia_rtx4000": {"nome": "NVIDIA GeForce RTX 4000", "no_escopo": False, "testada": False},
    "amd_outra": {"nome": "AMD (outra)", "no_escopo": False, "testada": False},
    "nvidia_outra": {"nome": "NVIDIA (outra)", "no_escopo": False, "testada": False},
    "sem_placa": {"nome": "Sem placa de vídeo compatível", "no_escopo": False, "testada": False},
}

# Modelos por nome interno (o mesmo de /models/status). OmniVoice não tem ModelConfig: usa "omnivoice".
PRINCIPAIS = ["qwen-tts-1.7B", "qwen-tts-0.6B", "qwen-custom-voice-1.7B", "qwen-custom-voice-0.6B",
              "chatterbox-tts", "omnivoice"]
SECUNDARIOS = ["chatterbox-turbo", "f5-tts", "kokoro", "luxtts", "qwen3-0.6b", "qwen3-1.7b", "qwen3-4b"]

AVISO_6GB_17B = "Placa de 6 GB: este modelo pode ficar lento ou falhar em blocos longos."
APERTADO_4GB = "Placa de 4 GB: cabe por pouco. Use voz de referência curta e feche outros programas que usam a placa."
NAO_CABE = "Não utilizável nesta placa: falta memória da placa de vídeo para este modelo."
GTX_SO_OMNI = "GTX 1000: só o OmniVoice roda nesta placa (os outros não cabem em float32)."
GTX_OMNI = "GTX 1000: OmniVoice em float16, não testado numa placa real. Deve ficar mais lento."
SEM_MEDIDA = "Não testado nesta família de placa."


def familia_por_nome(nome: str, arch: str = "", capacidade: Optional[tuple] = None, fabricante: str = "") -> str:
    """Família pelo nome da placa; a arquitetura (gfx da AMD, compute capability da NVIDIA) desempata.
    O nome vem primeiro porque o disfarce HSA_OVERRIDE_GFX_VERSION pode mudar o gfx informado."""
    n = (nome or "").upper()
    m = re.search(r"\bRX\s*([5-9])\d{3}", n)
    if m:
        return {"6": "amd_rx6000", "7": "amd_rx7000", "9": "amd_rx9000"}.get(m.group(1), "amd_outra")
    m = re.search(r"\b(GTX|RTX)\s*(\d{2})\d{2}", n)
    if m:
        serie = m.group(2)
        if m.group(1) == "GTX" and serie == "10":
            return "nvidia_gtx1000"
        if m.group(1) == "RTX" and serie == "30":
            return "nvidia_rtx3000"
        if m.group(1) == "RTX" and serie == "40":
            return "nvidia_rtx4000"
        return "nvidia_outra"
    a = (arch or "").lower()
    if a.startswith("gfx110"):
        return "amd_rx7000"
    if a.startswith("gfx103"):
        return "amd_rx6000"
    if a.startswith("gfx120"):
        return "amd_rx9000"
    if a.startswith("gfx"):
        return "amd_outra"
    if capacidade:
        if capacidade[0] == 6:
            return "nvidia_gtx1000"
        if tuple(capacidade[:2]) == (8, 6):
            return "nvidia_rtx3000"
        if tuple(capacidade[:2]) == (8, 9):
            return "nvidia_rtx4000"
        return "nvidia_outra"
    if "AMD" in n or "RADEON" in n or fabricante == "amd":
        return "amd_outra"
    if "NVIDIA" in n or "GEFORCE" in n or fabricante == "nvidia":
        return "nvidia_outra"
    return "sem_placa" if not n else "amd_outra" if fabricante == "amd" else "nvidia_outra"


def classe_vram(total_mb: Optional[float]) -> Optional[int]:
    """4, 6 ou 8 GB (8 = 8 GB ou mais). Placas de 3 GB entram na classe 4 (abaixo do mínimo)."""
    if not total_mb:
        return None
    if total_mb < 5000:
        return 4
    if total_mb < 7000:
        return 6
    return 8


def situacao_modelos(familia: str, classe: Optional[int]) -> dict:
    """{nome_do_modelo: {"situacao": ..., "motivo": ...}} conforme a família e a classe de VRAM."""
    res = {}

    def por(nome, situacao, motivo=""):
        res[nome] = {"situacao": situacao, "motivo": motivo}

    if familia == "sem_placa" or classe is None:
        for n in PRINCIPAIS + SECUNDARIOS:
            por(n, NAO_UTILIZAVEL, "Sem placa de vídeo compatível: o VozLab precisa de uma placa AMD RX 6000/7000 "
                                   "ou NVIDIA GTX 1000/RTX 3000.")
        return res

    if familia == "nvidia_gtx1000":
        for n in PRINCIPAIS + SECUNDARIOS:
            por(n, NAO_UTILIZAVEL, GTX_SO_OMNI)
        por("omnivoice", NAO_TESTADO, GTX_OMNI)
        return res

    sem_medida = "" if FAMILIAS.get(familia, {}).get("testada") else SEM_MEDIDA
    if classe >= 8:
        for n in PRINCIPAIS:
            por(n, OK, sem_medida)
    elif classe == 6:
        for n in PRINCIPAIS:
            por(n, OK, sem_medida)
        por("qwen-tts-1.7B", AVISO, AVISO_6GB_17B)
        por("qwen-custom-voice-1.7B", AVISO, AVISO_6GB_17B)
    else:  # 4 GB
        for n in ("qwen-tts-1.7B", "qwen-tts-0.6B", "qwen-custom-voice-1.7B", "chatterbox-tts"):
            por(n, NAO_UTILIZAVEL, NAO_CABE)
        por("qwen-custom-voice-0.6B", AVISO, APERTADO_4GB)
        por("omnivoice", AVISO, APERTADO_4GB)

    # Secundários: sem bateria de testes (decisão do usuário). Kokoro medido em 8 GB (pico 1,5 GB).
    for n in SECUNDARIOS:
        por(n, NAO_TESTADO, "Modelo secundário: não testado nas famílias de placa.")
    if classe >= 6:
        por("kokoro", OK, sem_medida)
    por("qwen3-4b", NAO_UTILIZAVEL, "Não utilizável em placas de até 8 GB (precisa de ~8 GB ou mais).")
    if classe <= 4:
        por("qwen3-1.7b", NAO_UTILIZAVEL, NAO_CABE)
    return res


def montar_perfil(nome: str, fabricante: str, arch: str = "", capacidade: Optional[tuple] = None,
                  total_mb: Optional[float] = None) -> dict:
    """Perfil completo a partir dos dados da placa (função pura, testável sem placa)."""
    familia = familia_por_nome(nome, arch, capacidade, fabricante) if nome or arch or capacidade else "sem_placa"
    classe = classe_vram(total_mb) if familia != "sem_placa" else None
    info = FAMILIAS.get(familia, FAMILIAS["amd_outra"])
    avisos = []
    if familia == "sem_placa":
        avisos.append("Não encontrei uma placa de vídeo compatível. O VozLab foi feito para rodar na placa de vídeo "
                      "(AMD RX 6000/7000 ou NVIDIA GTX 1000/RTX 3000); sem ela, a geração pode não funcionar ou "
                      "ficar muito lenta.")
    elif not info["no_escopo"]:
        avisos.append(f"Placa fora das famílias testadas ({info['nome']}). Ela pode funcionar, mas sem garantia.")
    elif not info["testada"]:
        avisos.append(f"Família {info['nome']}: perfil montado por simulação; ainda não testado numa placa real.")
    if classe == 4 and familia not in ("sem_placa",):
        avisos.append("Placa de 4 GB: só os modelos menores cabem.")
    return {
        "placa": nome or None, "fabricante": fabricante or None, "arquitetura": arch or None,
        "capacidade": list(capacidade) if capacidade else None,
        "vram_total_mb": round(total_mb) if total_mb else None, "classe_vram_gb": classe,
        "familia": familia, "familia_nome": info["nome"], "no_escopo": info["no_escopo"], "testada": info["testada"],
        "avisos": avisos, "modelos": situacao_modelos(familia, classe),
    }


_cache: Optional[dict] = None


def detectar(forcar: bool = False) -> dict:
    """Lê a placa pelo PyTorch (uma vez; depois usa o cache). Nunca levanta erro."""
    global _cache
    if _cache is not None and not forcar:
        return _cache
    try:
        import torch

        if not torch.cuda.is_available():
            _cache = montar_perfil("", "")
            return _cache
        props = torch.cuda.get_device_properties(0)
        amd = bool(getattr(torch.version, "hip", None))
        arch = (getattr(props, "gcnArchName", "") or "").split(":")[0] if amd else ""
        cap = None if amd else (props.major, props.minor)
        _cache = montar_perfil(props.name, "amd" if amd else "nvidia", arch, cap, props.total_memory / 2**20)
    except Exception as e:  # noqa: BLE001
        _cache = montar_perfil("", "")
        _cache["erro_deteccao"] = f"{type(e).__name__}: {str(e)[:200]}"
    return _cache

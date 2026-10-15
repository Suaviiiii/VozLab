"""
Preferências da tela (15/10/2026): modo qualidade, pausas, MP3 junto, mostrar o OmniVoice.

Antes ficavam só no navegador. Quando o VozLab abre no Edge em modo InPrivate (para o Edge não entrar
sozinho na conta Microsoft), o navegador esquece tudo ao fechar; por isso a tela também guarda uma cópia
aqui, em <dados>/config/preferencias_interface.json. Não guarda nada além dessas escolhas da tela.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter, Body

from .. import config

router = APIRouter(prefix="/preferencias-interface", tags=["preferencias"])
_trava = threading.Lock()
_PREFIXOS = ("vozlab_", "voicebox_")


def _arquivo() -> Path:
    return Path(config.get_data_dir()) / "config" / "preferencias_interface.json"


def ler() -> dict:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
        return {k: v for k, v in dados.items() if isinstance(v, str) and k.startswith(_PREFIXOS)}
    except Exception:
        return {}


def _gravar(prefs: dict) -> None:
    arq = _arquivo()
    arq.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(arq.parent), prefix=".preferencias_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(prefs, ensure_ascii=False, indent=2))
    os.replace(tmp, arq)  # troca atômica


@router.get("")
async def listar():
    return ler()


@router.put("")
async def atualizar(novas: dict = Body(...)):
    validas = {str(k): v for k, v in novas.items()
               if str(k).startswith(_PREFIXOS) and len(str(k)) <= 64 and isinstance(v, str) and len(v) <= 2000}
    with _trava:
        prefs = ler()
        prefs.update(validas)
        _gravar(prefs)
    return prefs

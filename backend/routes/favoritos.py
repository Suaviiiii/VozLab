"""
Vozes favoritas (pedido do usuário, 15/10/2026).

A lista fica num arquivo próprio, <dados>/config/favoritos.json, separado do banco: favoritar não altera
perfis, vozes nem histórico. As favoritas aparecem sempre no topo da tela Generate (e das outras listas).
"""

import json
import os
import tempfile
import threading
from pathlib import Path

from fastapi import APIRouter

from .. import config

router = APIRouter(prefix="/favoritos", tags=["favoritos"])
_trava = threading.Lock()


def _arquivo() -> Path:
    return Path(config.get_data_dir()) / "config" / "favoritos.json"


def ler() -> list[str]:
    try:
        dados = json.loads(_arquivo().read_text(encoding="utf-8"))
        ids = dados.get("ids", []) if isinstance(dados, dict) else []
        return [str(i) for i in ids if isinstance(i, str) and i]
    except FileNotFoundError:
        return []
    except Exception:
        return []


def _gravar(ids: list[str]) -> None:
    arq = _arquivo()
    arq.parent.mkdir(parents=True, exist_ok=True)
    conteudo = json.dumps({"versao": 1, "ids": ids}, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(dir=str(arq.parent), prefix=".favoritos_", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(conteudo)
    os.replace(tmp, arq)  # troca atômica: nunca fica um arquivo pela metade


@router.get("")
async def listar():
    return {"ids": ler()}


@router.put("/{profile_id}")
async def favoritar(profile_id: str):
    with _trava:
        ids = ler()
        if profile_id not in ids:
            ids.append(profile_id)
            _gravar(ids)
    return {"ids": ids}


@router.delete("/{profile_id}")
async def desfavoritar(profile_id: str):
    with _trava:
        ids = [i for i in ler() if i != profile_id]
        _gravar(ids)
    return {"ids": ids}

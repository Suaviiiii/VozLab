"""Rotas do importador de pacotes de vozes (VozLab). Só adições à API."""

import asyncio
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import voice_pack_import as vpi

router = APIRouter()

MAX_PACK_BYTES = 2 * 1024 * 1024 * 1024


class VoicePackImportRequest(BaseModel):
    token: str
    indices: Optional[List[int]] = None  # vazio = todas as vozes "novo"
    default_language: str = "pt"
    pack_name: Optional[str] = None


def _public(entries):
    return [{k: v for k, v in e.items() if k != "reference_text"} | {"has_transcript": bool(e.get("reference_text"))}
            for e in entries]


@router.post("/voice-packs/preview")
async def preview_voice_pack(
    file: UploadFile = File(...),
    default_language: str = Form("pt"),
    db: Session = Depends(get_db),
):
    """Lê um pacote .zip de vozes e mostra o que seria importado. Não grava nada no banco."""
    if default_language not in vpi.LANGS:
        raise HTTPException(status_code=422, detail="Idioma padrão inválido.")
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Escolha um arquivo .zip.")
    await asyncio.to_thread(vpi.expire_old_packs)
    dest = vpi.save_upload_stream_path()
    try:
        size = await asyncio.to_thread(vpi.copy_fileobj, file.file, dest, MAX_PACK_BYTES)
        entries = await asyncio.to_thread(vpi.scan_pack, dest, default_language)
        entries = await asyncio.to_thread(vpi.classify, dest, entries, db)
    except ValueError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # zip corrompido etc.
        dest.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"Não foi possível ler o pacote: {e}")
    counts = {s: sum(1 for e in entries if e.get("status") == s) for s in ("novo", "duplicado", "invalido")}
    return {"token": dest.stem, "pack_name": Path(file.filename or "pacote.zip").stem, "bytes": size,
            "total": len(entries), "counts": counts, "voices": _public(entries)}


@router.post("/voice-packs/import")
async def import_voice_pack(data: VoicePackImportRequest, db: Session = Depends(get_db)):
    """Importa as vozes novas de um pacote já enviado na prévia. Vozes existentes nunca são alteradas."""
    try:
        path = vpi.pack_path(data.token)
        entries = await asyncio.to_thread(vpi.scan_pack, path, data.default_language)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    label = (data.pack_name or path.stem)[:80]
    results = await vpi.import_entries(path, entries, data.indices, db, label)
    counts = {s: sum(1 for r in results if r["resultado"] == s) for s in ("importada", "pulada", "falhou")}
    return {"counts": counts, "results": results}

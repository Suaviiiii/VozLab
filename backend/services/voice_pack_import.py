"""
Importação de PACOTES de vozes (VozLab).

Um pacote é um .zip com qualquer mistura de:
  - áudios soltos (.wav .mp3 .flac .ogg .m4a .opus .aac .webm): cada áudio vira uma voz;
    um .txt com o mesmo nome (ao lado) é a transcrição do que é falado;
  - vozes exportadas pelo próprio programa: um .zip interno com manifest.json + samples.json,
    ou uma pasta com esses arquivos.

Fluxo em duas etapas (nada é gravado na prévia):
  1. preview: lê o pacote, valida cada áudio e marca duplicatas (nome já existe, ou
     impressão digital de uma importação anterior);
  2. importar: cria só as vozes "novas" escolhidas. Vozes existentes nunca são alteradas.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from .. import config
from ..database import VoiceProfile as DBVoiceProfile

AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".opus", ".aac", ".webm"}
LANGS = {"zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it", "he", "ar", "da", "el",
         "fi", "hi", "ms", "nl", "no", "pl", "sv", "sw", "tr"}
LANG_WORDS = {
    "portugues": "pt", "português": "pt", "ptbr": "pt", "brasil": "pt", "english": "en", "ingles": "en",
    "inglês": "en", "espanol": "es", "español": "es", "espanhol": "es", "frances": "fr", "francês": "fr",
    "french": "fr", "deutsch": "de", "alemao": "de", "alemão": "de", "german": "de", "italiano": "it",
    "italian": "it", "russo": "ru", "russian": "ru", "norsk": "no", "noruegues": "no", "norueguês": "no",
    "japones": "ja", "japonês": "ja", "japanese": "ja", "ale": "de",
}
FEM = {"feminino", "feminina", "female", "mulher", "woman", "f", "fem"}
MASC = {"masculino", "masculina", "male", "homem", "man", "m", "masc"}

MAX_ENTRIES = 5000
MAX_MEMBER_BYTES = 80 * 1024 * 1024       # um áudio ou zip interno
MAX_TOTAL_BYTES = 4 * 1024 * 1024 * 1024  # descompactado no total
MAX_VOICES = 1000
PACK_TTL_S = 6 * 3600
FINGERPRINT_RE = re.compile(r"impressão ([0-9a-f]{12})")


def packs_dir() -> Path:
    d = config.get_data_dir() / "cache" / "pacotes_vozes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def pack_path(token: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", token or ""):
        raise ValueError("Identificador do pacote inválido.")
    p = packs_dir() / f"{token}.zip"
    if not p.is_file():
        raise ValueError("Pacote não encontrado ou expirado. Envie o arquivo de novo.")
    return p


def expire_old_packs() -> None:
    """Pacotes enviados e não importados ficam 6 h no cache do próprio importador."""
    now = time.time()
    for p in packs_dir().glob("*.zip"):
        try:
            if now - p.stat().st_mtime > PACK_TTL_S:
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------- leitura

def _tokens(path: str) -> List[str]:
    return [t for t in re.split(r"[\s_\-\[\]\(\)./\\]+", path.lower()) if t]


def guess_language(path: str, default: str) -> str:
    for tok in reversed(_tokens(path)):
        if tok in LANGS:
            return tok
        if tok in LANG_WORDS:
            return LANG_WORDS[tok]
    return default


def guess_gender(path: str) -> Optional[str]:
    toks = set(_tokens(path))
    if toks & FEM and not toks & MASC:
        return "FEMALE"
    if toks & MASC and not toks & FEM:
        return "MALE"
    return None


def clean_name(stem: str) -> str:
    name = re.sub(r"[_]+", " ", stem).strip()
    name = re.sub(r"\s+", " ", name)
    return name[:100] or "Voz importada"


def _check_zip(zf: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    infos = [i for i in zf.infolist() if not i.is_dir()]
    if len(infos) > MAX_ENTRIES:
        raise ValueError(f"O pacote tem arquivos demais ({len(infos)}; máximo {MAX_ENTRIES}).")
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise ValueError("O pacote descompactado passa de 4 GB.")
    return infos


def _norm(name: str) -> str:
    # Caminho dentro do zip, sem "..", sem barra inicial, separador "/".
    parts = [p for p in PurePosixPath(name.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    return "/".join(parts)


def _is_export_zip(data: bytes) -> Optional[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = set(z.namelist())
            if "manifest.json" in names and "samples.json" in names:
                manifest = json.loads(z.read("manifest.json"))
                samples = json.loads(z.read("samples.json"))
                if isinstance(manifest, dict) and isinstance(manifest.get("profile"), dict) and isinstance(samples, dict):
                    return {"manifest": manifest, "samples": samples}
    except Exception:
        return None
    return None


def _folder_as_zip(zf: zipfile.ZipFile, prefix: str, infos: List[zipfile.ZipInfo]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as out:
        for i in infos:
            n = _norm(i.filename)
            if n.startswith(prefix) and i.file_size <= MAX_MEMBER_BYTES:
                out.writestr(n[len(prefix):], zf.read(i))
    return buf.getvalue()


def scan_pack(path: Path, default_language: str = "pt") -> List[Dict[str, Any]]:
    """Lista as vozes candidatas do pacote (sem validar áudio nem consultar o banco)."""
    entries: List[Dict[str, Any]] = []
    with zipfile.ZipFile(path) as zf:
        infos = _check_zip(zf)
        by_name = {_norm(i.filename): i for i in infos}

        # 1) Pastas no formato exportado (manifest.json + samples.json lado a lado).
        export_prefixes = set()
        for n in by_name:
            if n.endswith("manifest.json"):
                prefix = n[: -len("manifest.json")]
                if prefix + "samples.json" in by_name:
                    export_prefixes.add(prefix)
        for prefix in sorted(export_prefixes):
            data = _folder_as_zip(zf, prefix, infos)
            info = _is_export_zip(data)
            if info:
                entries.append(_export_entry(prefix or path.stem, data, info, default_language))

        # 2) Zips internos no formato exportado, e 3) áudios soltos.
        for n, i in sorted(by_name.items()):
            if any(n.startswith(p) for p in export_prefixes):
                continue
            ext = PurePosixPath(n).suffix.lower()
            if ext == ".zip":
                if i.file_size > MAX_MEMBER_BYTES:
                    entries.append(_invalid(n, "Zip interno grande demais."))
                    continue
                data = zf.read(i)
                info = _is_export_zip(data)
                if info:
                    entries.append(_export_entry(n, data, info, default_language))
                else:
                    entries.append(_invalid(n, "Zip interno não é uma voz exportada (sem manifest.json/samples.json)."))
            elif ext in AUDIO_EXTS:
                if i.file_size > MAX_MEMBER_BYTES:
                    entries.append(_invalid(n, "Áudio grande demais."))
                    continue
                stem = PurePosixPath(n).stem
                txt = None
                for cand in (n[: -len(ext)] + ".txt", n + ".txt"):
                    if cand in by_name and by_name[cand].file_size < 20000:
                        txt = zf.read(by_name[cand]).decode("utf-8", errors="replace").strip()
                        break
                data = zf.read(i)
                entries.append({
                    "kind": "audio",
                    "source": n,
                    "name": clean_name(stem),
                    "language": guess_language(n, default_language),
                    "gender": guess_gender(n),
                    "samples": 1,
                    "reference_text": txt or "",
                    "fingerprint": hashlib.sha256(data).hexdigest()[:12],
                })
            if len(entries) > MAX_VOICES:
                raise ValueError(f"O pacote tem mais de {MAX_VOICES} vozes. Divida em pacotes menores.")
    for idx, e in enumerate(entries, start=1):
        e["idx"] = idx
    return entries


def _export_entry(source: str, data: bytes, info: dict, default_language: str) -> Dict[str, Any]:
    prof = info["manifest"]["profile"]
    lang = prof.get("language") if prof.get("language") in LANGS else default_language
    return {
        "kind": "exportada",
        "source": source,
        "name": clean_name(str(prof.get("name") or PurePosixPath(source).stem)),
        "language": lang,
        "gender": prof.get("personality") if prof.get("personality") in ("FEMALE", "MALE") else None,
        "samples": len(info["samples"]),
        "reference_text": None,
        "fingerprint": hashlib.sha256(data).hexdigest()[:12],
    }


def _invalid(source: str, reason: str) -> Dict[str, Any]:
    return {"kind": "invalido", "source": source, "name": PurePosixPath(source).name, "language": None,
            "gender": None, "samples": 0, "reference_text": None, "fingerprint": None,
            "status": "invalido", "motivo": reason}


# ---------------------------------------------------------------------- prévia

def _existing(db: Session):
    names = set()
    prints = set()
    for name, desc in db.query(DBVoiceProfile.name, DBVoiceProfile.description).all():
        names.add((name or "").strip().casefold())
        m = FINGERPRINT_RE.search(desc or "")
        if m:
            prints.add(m.group(1))
    return names, prints


def _validate_audio_bytes(data: bytes, suffix: str):
    from ..utils.audio import validate_and_load_reference_audio
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        ok, err, audio, sr = validate_and_load_reference_audio(tmp_path)
        dur = round(len(audio) / sr, 1) if ok and audio is not None and sr else None
        return ok, err, dur
    finally:
        Path(tmp_path).unlink(missing_ok=True)


AUDIO_ERRORS = {
    "Audio too short": "Áudio curto demais (mínimo 2 s).",
    "Audio too long": "Áudio longo demais (máximo 30 s).",
    "Audio is too quiet or silent": "Áudio baixo demais ou em silêncio.",
}


def classify(path: Path, entries: List[Dict[str, Any]], db: Session) -> List[Dict[str, Any]]:
    """Marca cada voz como novo / duplicado / invalido. Não grava nada."""
    names, prints = _existing(db)
    seen_names, seen_prints = set(), set()
    with zipfile.ZipFile(path) as zf:
        by_name = {_norm(i.filename): i for i in zf.infolist() if not i.is_dir()}
        for e in entries:
            if e.get("status") == "invalido":
                continue
            e["duration_s"] = None
            key = e["name"].casefold()
            if e["fingerprint"] in prints:
                e["status"], e["motivo"] = "duplicado", "Já importada antes (mesma impressão digital)."
            elif key in names:
                e["status"], e["motivo"] = "duplicado", "Já existe uma voz com este nome na biblioteca."
            elif e["fingerprint"] in seen_prints:
                e["status"], e["motivo"] = "duplicado", "Arquivo repetido dentro do próprio pacote."
            elif key in seen_names:
                e["status"], e["motivo"] = "duplicado", "Nome repetido dentro do próprio pacote."
            else:
                e["status"], e["motivo"] = "novo", ""
            if e["kind"] == "audio" and e["status"] == "novo":
                data = zf.read(by_name[e["source"]])
                ok, err, dur = _validate_audio_bytes(data, PurePosixPath(e["source"]).suffix.lower())
                e["duration_s"] = dur
                if not ok:
                    msg = next((v for k, v in AUDIO_ERRORS.items() if (err or "").startswith(k)), err or "Áudio inválido.")
                    e["status"], e["motivo"] = "invalido", msg
                elif not e["reference_text"]:
                    e["motivo"] = "Sem transcrição (.txt): o clone pode ficar menos fiel."
            seen_names.add(key)
            if e["fingerprint"]:
                seen_prints.add(e["fingerprint"])
    return entries


# ---------------------------------------------------------------------- importação

async def import_entries(path: Path, entries: List[Dict[str, Any]], wanted: Optional[List[int]],
                         db: Session, pack_label: str) -> List[Dict[str, Any]]:
    """Cria as vozes 'novo' (filtradas por `wanted`, se dado). Reclassifica antes, contra o banco atual."""
    from ..models import VoiceProfileCreate
    from . import export_import, profiles as profiles_svc

    entries = classify(path, entries, db)
    want = set(wanted) if wanted else None
    results = []
    with zipfile.ZipFile(path) as zf:
        by_name = {_norm(i.filename): i for i in zf.infolist() if not i.is_dir()}
        for e in entries:
            if want is not None and e["idx"] not in want:
                continue
            await asyncio.sleep(0)  # deixa o servidor respirar entre uma voz e outra
            res = {"idx": e["idx"], "name": e["name"], "source": e["source"]}
            if e.get("status") != "novo":
                res.update(resultado="pulada", motivo=e.get("motivo", ""))
                results.append(res)
                continue
            tag = f"Importada do pacote {pack_label} (impressão {e['fingerprint']}). Fonte: {pack_label}."
            created_id = None
            try:
                if e["kind"] == "audio":
                    prof = await profiles_svc.create_profile(VoiceProfileCreate(
                        name=e["name"], description=tag[:500], language=e["language"],
                    ), db)
                    created_id = prof.id
                    if e["gender"]:
                        row = db.query(DBVoiceProfile).filter_by(id=prof.id).first()
                        row.personality = e["gender"]
                        db.commit()
                    suffix = PurePosixPath(e["source"]).suffix.lower()
                    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                        tmp.write(zf.read(by_name[e["source"]]))
                        tmp_path = tmp.name
                    try:
                        await profiles_svc.add_profile_sample(prof.id, tmp_path, e["reference_text"] or "", db)
                    finally:
                        Path(tmp_path).unlink(missing_ok=True)
                else:
                    data = (zf.read(by_name[e["source"]]) if e["source"] in by_name
                            else _folder_as_zip(zf, e["source"], list(zf.infolist())))
                    prof = await export_import.import_profile_from_zip(data, db)
                    created_id = prof.id
                    row = db.query(DBVoiceProfile).filter_by(id=prof.id).first()
                    row.description = ((row.description or "") + " " + tag).strip()[:500]
                    db.commit()
                res.update(resultado="importada", profile_id=created_id)
            except Exception as ex:  # noqa: BLE001
                db.rollback()
                res.update(resultado="falhou", motivo=str(ex))
                if created_id:
                    # Desfaz só a voz que ESTA importação acabou de criar (ficaria sem amostra).
                    try:
                        await profiles_svc.delete_profile(created_id, db)
                        res["motivo"] += " (voz incompleta desfeita)"
                    except Exception:
                        res["motivo"] += f" (atenção: voz {created_id} ficou incompleta)"
            results.append(res)
    return results


def save_upload_stream_path() -> Path:
    return packs_dir() / f"{uuid.uuid4().hex}.zip"


def copy_fileobj(src, dst_path: Path, limit: int) -> int:
    total = 0
    with open(dst_path, "wb") as out:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("Pacote grande demais (máximo 2 GB).")
            out.write(chunk)
    return total

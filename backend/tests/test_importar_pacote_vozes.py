"""
VozLab: importador de pacotes de vozes (/voice-packs/preview e /voice-packs/import).

Banco e pasta de dados TEMPORÁRIOS; nada toca a biblioteca real.
Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_importar_pacote_vozes.py" -t .
"""

import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from backend import config  # noqa: E402
from backend.database import session as db_session  # noqa: E402
from backend.database import VoiceProfile, ProfileSample  # noqa: E402
import backend.app  # noqa: E402,F401  (evita a importação circular já existente profiles <-> app)
from backend.routes import voice_packs, profiles as profiles_routes  # noqa: E402

SR = 24000


def wav_bytes(seconds: float, seed: int, fmt: str = "WAV") -> bytes:
    rng = np.random.default_rng(seed)
    t = np.arange(int(SR * seconds)) / SR
    audio = (0.2 * np.sin(2 * np.pi * (180 + seed) * t) + 0.02 * rng.standard_normal(t.size)).astype(np.float32)
    buf = io.BytesIO()
    sf.write(buf, audio, SR, format=fmt)
    return buf.getvalue()


def export_zip(name: str, lang: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", json.dumps({"version": "1.0", "profile": {"name": name, "language": lang}}))
        z.writestr("samples.json", json.dumps({"s1.wav": "bonjour tout le monde"}))
        z.writestr("samples/s1.wav", wav_bytes(3.0, 7))
    return buf.getvalue()


def montar_pacote() -> bytes:
    ana = wav_bytes(3.0, 1)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("pt/feminino/Ana_Clara.wav", ana)
        z.writestr("pt/feminino/Ana_Clara.txt", "Olá, eu sou a Ana Clara.")
        z.writestr("pt/feminino/Ana_Clara_copia.wav", ana)                 # mesmo áudio: duplicata no pacote
        z.writestr("en/male/John_Smith.flac", wav_bytes(4.0, 2, "FLAC"))
        z.writestr("curto.wav", wav_bytes(1.0, 3))                    # curto demais
        z.writestr("Nome_Existente.wav", wav_bytes(3.0, 4))           # já existe no banco
        z.writestr("exportadas/marie.zip", export_zip("Marie Export", "fr"))
        z.writestr("lixo/qualquer.zip", b"PK\x05\x06" + b"\x00" * 18)  # zip vazio, não é voz
        z.writestr("leia-me.txt", "pacote de teste")
    return buf.getvalue()


class ImportarPacoteTest(unittest.TestCase):
    def setUp(self):
        self._old = config.get_data_dir()
        self.tmp = Path(tempfile.mkdtemp(prefix="vb_pacote_"))
        config.set_data_dir(self.tmp)
        db_session.init_db()
        db = db_session.SessionLocal()
        db.add(VoiceProfile(id="existente", name="Nome Existente", language="pt", description="original"))
        db.commit()
        db.close()
        app = FastAPI()
        app.include_router(profiles_routes.router)
        app.include_router(voice_packs.router)
        self.client = TestClient(app)
        self.pacote = montar_pacote()

    def tearDown(self):
        db_session.engine.dispose()
        config.set_data_dir(self._old)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def preview(self):
        r = self.client.post("/voice-packs/preview", data={"default_language": "pt"},
                             files={"file": ("meu_pacote.zip", self.pacote, "application/zip")})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()

    def perfis(self):
        db = db_session.SessionLocal()
        try:
            return {p.name: p for p in db.query(VoiceProfile).all()}
        finally:
            db.close()

    def test_previa_nao_grava_e_classifica(self):
        antes = set(self.perfis())
        p = self.preview()
        por_nome = {v["name"]: v for v in p["voices"]}
        self.assertEqual(set(self.perfis()), antes, "a prévia não pode gravar no banco")
        self.assertEqual(por_nome["Ana Clara"]["status"], "novo")
        self.assertEqual(por_nome["Ana Clara"]["language"], "pt")
        self.assertEqual(por_nome["Ana Clara"]["gender"], "FEMALE")
        self.assertTrue(por_nome["Ana Clara"]["has_transcript"])
        self.assertEqual(por_nome["Ana Clara copia"]["status"], "duplicado")
        self.assertEqual(por_nome["John Smith"]["language"], "en")
        self.assertEqual(por_nome["John Smith"]["gender"], "MALE")
        self.assertEqual(por_nome["John Smith"]["status"], "novo")
        self.assertEqual(por_nome["curto"]["status"], "invalido")
        self.assertIn("curto demais", por_nome["curto"]["motivo"])
        self.assertEqual(por_nome["Nome Existente"]["status"], "duplicado")
        self.assertEqual(por_nome["Marie Export"]["kind"], "exportada")
        self.assertEqual(por_nome["Marie Export"]["status"], "novo")
        self.assertEqual(por_nome["qualquer.zip"]["status"], "invalido")
        self.assertEqual(p["counts"], {"novo": 3, "duplicado": 2, "invalido": 2})

    def test_importa_so_as_novas_e_nao_toca_existente(self):
        p = self.preview()
        r = self.client.post("/voice-packs/import", json={"token": p["token"], "pack_name": p["pack_name"]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["counts"], {"importada": 3, "pulada": 4, "falhou": 0}, r.text)
        perfis = self.perfis()
        self.assertEqual(set(perfis), {"Nome Existente", "Ana Clara", "John Smith", "Marie Export"})
        self.assertEqual(perfis["Nome Existente"].description, "original")
        self.assertEqual(perfis["Ana Clara"].personality, "FEMALE")
        self.assertEqual(perfis["Marie Export"].language, "fr")
        self.assertIn("Fonte: meu_pacote.", perfis["Ana Clara"].description)
        db = db_session.SessionLocal()
        try:
            amostra = db.query(ProfileSample).filter_by(profile_id=perfis["Ana Clara"].id).one()
            self.assertEqual(amostra.reference_text, "Olá, eu sou a Ana Clara.")
        finally:
            db.close()

    def test_reimportar_o_mesmo_pacote_nao_duplica(self):
        p = self.preview()
        self.client.post("/voice-packs/import", json={"token": p["token"], "pack_name": p["pack_name"]})
        n = len(self.perfis())
        p2 = self.preview()
        self.assertEqual(p2["counts"]["novo"], 0)
        r = self.client.post("/voice-packs/import", json={"token": p2["token"], "pack_name": p2["pack_name"]})
        self.assertEqual(r.json()["counts"]["importada"], 0)
        self.assertEqual(len(self.perfis()), n)

    def test_escolher_so_algumas(self):
        p = self.preview()
        ana = next(v["idx"] for v in p["voices"] if v["name"] == "Ana Clara")
        r = self.client.post("/voice-packs/import", json={"token": p["token"], "indices": [ana]})
        self.assertEqual(r.json()["counts"]["importada"], 1)
        self.assertIn("Ana Clara", self.perfis())
        self.assertNotIn("John Smith", self.perfis())

    def test_erros_claros(self):
        r = self.client.post("/voice-packs/preview", files={"file": ("x.txt", b"oi", "text/plain")})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/voice-packs/preview", files={"file": ("x.zip", b"nao e zip", "application/zip")})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/voice-packs/import", json={"token": "../../etc"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/voice-packs/import", json={"token": "0" * 32})
        self.assertEqual(r.status_code, 400)
        self.assertIn("expirado", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()

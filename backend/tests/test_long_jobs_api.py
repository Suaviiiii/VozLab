"""
Testes de API da Fase 3b-2 (rotas aditivas de texto longo).

Banco SQLite e pasta de dados temporários; motor TTS FALSO e determinístico
(sem GPU, sem modelo). O caminho real de produção (fila serial, run_job,
manifesto, montagem, versões) roda de verdade.

Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_long_jobs_api.py" -t .
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from backend import config  # noqa: E402
from backend.database import session as db_session  # noqa: E402
from backend.database import Generation, GenerationVersion, VoiceProfile  # noqa: E402
from backend.routes import generations as gen_routes  # noqa: E402
from backend.services import long_job_ops, task_queue  # noqa: E402
from backend.services.long_jobs import (  # noqa: E402
    STATUS_EM_ANDAMENTO,
    STATUS_INTERROMPIDO,
    JobLock,
    ManifestManager,
    get_job_dir,
)
from backend.utils.audio import normalize_audio  # noqa: E402
from backend.utils.chunked_tts import concatenate_audio_chunks  # noqa: E402

SR = 24000
PROFILE_ID = "perfil-teste-kokoro"

TEXTO_LONGO = (
    "O primeiro parágrafo fala sobre a chuva que caiu na cidade durante a noite inteira. "
    "As ruas ficaram alagadas e os carros mal conseguiam passar pelas avenidas principais.\n\n"
    "No segundo parágrafo, o sol voltou a aparecer de manhã cedo e secou as calçadas. "
    "As crianças saíram para brincar na praça, correndo atrás dos pombos e das bolhas.\n\n"
    "Por fim, o terceiro parágrafo encerra a história com o pôr do sol alaranjado. "
    "Todos voltaram para casa cansados, mas felizes com o dia que tiveram juntos."
)


# ---------------------------------------------------------------------------
# Motor falso
# ---------------------------------------------------------------------------

class FakeEngine:
    """Sintetizador determinístico: mesmo texto + semente => mesmas amostras."""

    def __init__(self):
        self.calls: list[tuple[str, object]] = []
        self.fail_on_call: int | None = None

    def is_loaded(self):
        return True

    async def synth(self, backend=None, chunk_text="", voice_prompt=None, language="en",
                    chunk_seed=None, instruct=None, trim_fn=None, runaway_detector=None,
                    crossfade_ms=50, retry_depth=0):
        self.calls.append((chunk_text, chunk_seed))
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise RuntimeError("falha simulada do motor")
        key = hashlib.sha256(f"{chunk_text}|{chunk_seed}".encode("utf-8")).digest()
        rng = np.random.default_rng(int.from_bytes(key[:8], "little"))
        n = SR // 5 + len(chunk_text) * 40
        return (rng.standard_normal(n).astype(np.float32) * 0.1), SR


@contextlib.contextmanager
def fake_engine_patches(engine: FakeEngine):
    async def _noop(*a, **k):
        return None

    async def _prompt(*a, **k):
        return {}

    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch("backend.backends.get_tts_backend_for_engine", lambda e: engine))
        stack.enter_context(mock.patch("backend.backends.load_engine_model", _noop))
        stack.enter_context(mock.patch("backend.backends.engine_needs_trim", lambda e: False))
        stack.enter_context(mock.patch("backend.backends.engine_retries_runaway", lambda e: False))
        stack.enter_context(mock.patch("backend.services.profiles.create_voice_prompt_for_profile", _prompt))
        stack.enter_context(mock.patch("backend.services.long_generation.generate_single_chunk", engine.synth))
        stack.enter_context(mock.patch("backend.utils.chunked_tts.generate_single_chunk", engine.synth))
        yield


def read_samples(path: Path) -> np.ndarray:
    audio, _ = sf.read(str(path), dtype="float32")
    return audio


# ---------------------------------------------------------------------------
# Base: pasta e banco temporários + app mínimo com a fila real
# ---------------------------------------------------------------------------

class ApiTestBase(unittest.TestCase):
    flags_on = True

    def setUp(self):
        self._old_data_dir = config.get_data_dir()
        self.tmp = Path(tempfile.mkdtemp(prefix="vb_api_"))
        config.set_data_dir(self.tmp)
        db_session.init_db()
        # Flags explícitas: desde ATIVAR-V2-PADRAO as três vêm true de fábrica.
        # Estes testes medem as emendas da Fase 3 (emendas_v2 desligada) e, em
        # TestFlagsDesligadas, o caminho com tudo desligado.
        cfg_path = self.tmp / "config" / "texto_longo.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"texto_longo_v2": self.flags_on,
                                        "persistencia_jobs": self.flags_on,
                                        "emendas_v2": False}), encoding="utf-8")

        db = db_session.SessionLocal()
        db.add(VoiceProfile(id=PROFILE_ID, name="Kokoro Teste", language="pt",
                            voice_type="preset", preset_engine="kokoro", preset_voice_id="pf_dora"))
        db.commit()
        db.close()

        self.engine = FakeEngine()
        self._patches = fake_engine_patches(self.engine)
        self._patches.__enter__()

        app = FastAPI()

        @app.on_event("startup")
        async def _start_queue():
            task_queue._generation_worker_task = None
            task_queue.init_queue()

        app.include_router(gen_routes.router)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        # Espera a fila esvaziar: fechar o laço com job rodando prende o worker.
        t0 = time.time()
        while (task_queue._running_generation_tasks or task_queue._queued_generation_ids) and time.time() - t0 < 30:
            time.sleep(0.05)
        self.client.__exit__(None, None, None)
        self._patches.__exit__(None, None, None)
        db_session.engine.dispose()
        config.set_data_dir(self._old_data_dir)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- utilitários -------------------------------------------------------
    def db(self):
        return db_session.SessionLocal()

    def gen_row(self, gid):
        db = self.db()
        try:
            return db.query(Generation).filter_by(id=gid).first()
        finally:
            db.close()

    def wait_done(self, gid, timeout=30.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            row = self.gen_row(gid)
            if row is not None and row.status not in ("generating", "loading_model"):
                return row
            time.sleep(0.05)
        self.fail(f"geração {gid} não terminou em {timeout}s")

    def generate(self, text=TEXTO_LONGO, seed=42):
        r = self.client.post("/generate", json={
            "profile_id": PROFILE_ID, "text": text, "language": "pt", "engine": "kokoro",
            "seed": seed, "max_chunk_chars": 100,
        })
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["id"]

    def manifest(self, gid):
        return json.loads((get_job_dir(self.tmp, gid) / "manifest.json").read_text(encoding="utf-8"))

    def versions(self, gid):
        db = self.db()
        try:
            return db.query(GenerationVersion).filter_by(generation_id=gid).count()
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Fluxo completo
# ---------------------------------------------------------------------------

class TestFluxoCompleto(ApiTestBase):

    def test_01_generate_progress_blocks_audio(self):
        gid = self.generate()
        row = self.wait_done(gid)
        self.assertEqual(row.status, "completed", row.error)

        m = self.manifest(gid)
        n = m["total_chunks"]
        self.assertGreaterEqual(n, 3)
        self.assertEqual(m["job_status"], "concluido")
        self.assertEqual(m["generation_parameters"]["base_seed"], 42)
        self.assertTrue(m["generation_parameters"]["normalize"])
        self.assertEqual(m["generation_parameters"]["crossfade_ms"], 50)

        p = self.client.get(f"/generate/{gid}/progress").json()
        self.assertEqual((p["completed_chunks"], p["total_chunks"], p["percent"]), (n, n, 100.0))
        self.assertFalse(p["is_locked"])
        self.assertFalse(p["can_resume"])

        b = self.client.get(f"/generate/{gid}/blocks").json()
        self.assertEqual([c["idx"] for c in b["blocks"]], list(range(1, n + 1)))

        # idx 1-based: 0 e N+1 => 404; 1 e N => 200 com as amostras do disco
        self.assertEqual(self.client.get(f"/generate/{gid}/blocks/0/audio").status_code, 404)
        self.assertEqual(self.client.get(f"/generate/{gid}/blocks/{n + 1}/audio").status_code, 404)
        self.assertEqual(self.client.get(f"/generate/{gid}/blocks/-1/audio").status_code, 404)
        for idx in (1, n):
            r = self.client.get(f"/generate/{gid}/blocks/{idx}/audio")
            self.assertEqual(r.status_code, 200)
            got, _ = sf.read(__import__("io").BytesIO(r.content), dtype="float32")
            disk = read_samples(get_job_dir(self.tmp, gid) / f"chunk_{idx:02d}.wav")
            self.assertTrue(np.array_equal(got, disk))

    def test_02_reassemble_identico_ao_master(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        master_original = read_samples(config.resolve_storage_path(self.gen_row(gid).audio_path))
        v_antes = self.versions(gid)

        self.assertEqual(self.client.post(f"/generate/{gid}/reassemble",
                                          json={"pause_paragrafo_ms": 1000}).status_code, 422)
        self.assertEqual(self.client.post(f"/generate/{gid}/reassemble",
                                          json={"target_lufs": -14}).status_code, 422)

        r = self.client.post(f"/generate/{gid}/reassemble", json={"pause_paragrafo_ms": 775})
        self.assertEqual(r.status_code, 200, r.text)
        novo = self.gen_row(gid)
        self.assertNotEqual(novo.audio_path, None)
        remontado = read_samples(config.resolve_storage_path(novo.audio_path))
        self.assertTrue(np.array_equal(remontado, master_original))
        self.assertEqual(self.versions(gid), v_antes + 1)

        # Conferência independente: mesmo juntador + mesma normalização
        chunks = [read_samples(get_job_dir(self.tmp, gid) / f"chunk_{i:02d}.wav")
                  for i in range(1, self.manifest(gid)["total_chunks"] + 1)]
        esperado = normalize_audio(concatenate_audio_chunks(chunks, SR, crossfade_ms=50))
        esperado_salvo = read_samples(config.resolve_storage_path(novo.audio_path))
        self.assertEqual(len(esperado), len(esperado_salvo))

    def test_03_regenerar_bloco_preserva_anterior(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        job_dir = get_job_dir(self.tmp, gid)
        antigo = read_samples(job_dir / "chunk_02.wav")
        outros = {i: read_samples(job_dir / f"chunk_{i:02d}.wav") for i in (1, 3)}
        v_antes = self.versions(gid)
        n_calls = len(self.engine.calls)

        r = self.client.post(f"/generate/{gid}/blocks/2/regenerate", json={"seed": 777})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.wait_done(gid)
        self.assertEqual(row.status, "completed", row.error)
        self.assertIsNone(row.error)

        self.assertEqual(len(self.engine.calls), n_calls + 1)
        self.assertEqual(self.engine.calls[-1][1], 777)
        self.assertTrue(np.array_equal(read_samples(job_dir / "chunk_02.a1.wav"), antigo))
        self.assertFalse(np.array_equal(read_samples(job_dir / "chunk_02.wav"), antigo))
        for i, a in outros.items():
            self.assertTrue(np.array_equal(read_samples(job_dir / f"chunk_{i:02d}.wav"), a))
        bloco = self.manifest(gid)["chunks"][1]
        self.assertEqual([a["seed"] for a in bloco["attempts"]], [43, 777])
        self.assertEqual(self.versions(gid), v_antes + 1)
        self.assertEqual(self.manifest(gid)["job_status"], "concluido")

    def test_04_regenerate_arquiva_pasta_e_usa_semente_nova(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        r = self.client.post(f"/generate/{gid}/regenerate")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.wait_done(gid).status, "completed")
        arquivadas = [p.name for p in (self.tmp / "jobs").iterdir() if p.name.startswith(f"{gid}_arquivado_")]
        self.assertEqual(len(arquivadas), 1)
        self.assertTrue((self.tmp / "jobs" / arquivadas[0] / "chunk_01.wav").exists())
        m = self.manifest(gid)
        self.assertIsNone(m["generation_parameters"]["base_seed"])
        self.assertEqual(m["job_status"], "concluido")

    def test_05_delete_job_files(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        master = config.resolve_storage_path(self.gen_row(gid).audio_path)

        self.assertEqual(self.client.delete(f"/generate/{gid}/job-files").status_code, 400)
        with JobLock(self.tmp, gid):
            self.assertEqual(self.client.delete(f"/generate/{gid}/job-files?confirm=true").status_code, 409)
        self.assertTrue(get_job_dir(self.tmp, gid).exists())

        st = self.client.get("/system/jobs/storage").json()
        self.assertEqual(st["jobs"], 1)
        self.assertGreater(st["total_bytes"], 0)

        r = self.client.delete(f"/generate/{gid}/job-files?confirm=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(r.json()["freed_bytes"], 0)
        self.assertFalse(get_job_dir(self.tmp, gid).exists())
        self.assertTrue(master.is_file())
        self.assertEqual(self.client.get(f"/generate/{gid}/progress").status_code, 404)
        self.assertEqual(self.client.get("/system/jobs/storage").json()["jobs"], 0)


# ---------------------------------------------------------------------------
# Falha, retomada e retry
# ---------------------------------------------------------------------------

class TestFalhaERetomada(ApiTestBase):

    def _gera_com_falha_no_bloco_3(self):
        self.engine.fail_on_call = 3
        gid = self.generate()
        row = self.wait_done(gid)
        self.assertEqual(row.status, "failed")
        self.assertIn("falha simulada", row.error or "")
        self.engine.fail_on_call = None
        m = self.manifest(gid)
        self.assertEqual(m["job_status"], "falhou")
        self.assertEqual(m["completed_chunks"], 2)
        return gid, m

    def test_01_erro_do_motor_nao_prende_em_generating(self):
        self._gera_com_falha_no_bloco_3()

    def test_02_resume_gera_so_os_blocos_que_faltam(self):
        gid, m = self._gera_com_falha_no_bloco_3()
        n = m["total_chunks"]
        job_dir = get_job_dir(self.tmp, gid)
        antes = {i: read_samples(job_dir / f"chunk_{i:02d}.wav") for i in (1, 2)}
        p = self.client.get(f"/generate/{gid}/progress").json()
        self.assertTrue(p["can_resume"])
        self.assertEqual(p["completed_chunks"], 2)

        self.engine.calls.clear()
        r = self.client.post(f"/generate/{gid}/resume", json={"text": TEXTO_LONGO, "language": "pt"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.wait_done(gid)
        self.assertEqual(row.status, "completed", row.error)

        textos = [c["text"] for c in m["chunks"]]
        self.assertEqual([t for t, _ in self.engine.calls], textos[2:])
        self.assertEqual([s for _, s in self.engine.calls], [42 + i for i in range(2, n)])
        for i, a in antes.items():
            self.assertTrue(np.array_equal(read_samples(job_dir / f"chunk_{i:02d}.wav"), a))
        self.assertEqual(self.manifest(gid)["job_status"], "concluido")

        # Mesmo áudio de uma geração contínua com a mesma semente
        gid2 = self.generate()
        self.assertEqual(self.wait_done(gid2).status, "completed")
        a1 = read_samples(config.resolve_storage_path(self.gen_row(gid).audio_path))
        a2 = read_samples(config.resolve_storage_path(self.gen_row(gid2).audio_path))
        self.assertTrue(np.array_equal(a1, a2))

    def test_03_resume_recusa_divergencias(self):
        gid, _ = self._gera_com_falha_no_bloco_3()
        self.assertEqual(self.client.post(f"/generate/{gid}/resume", json={"text": "outro texto"}).status_code, 409)
        self.assertEqual(self.client.post(f"/generate/{gid}/resume", json={"engine": "qwen"}).status_code, 409)
        self.assertEqual(self.client.post(f"/generate/{gid}/resume", json={"language": "en"}).status_code, 409)
        with JobLock(self.tmp, gid):
            self.assertEqual(self.client.post(f"/generate/{gid}/resume").status_code, 409)
        # Áudio de referência alterado (hash do manifesto diverge do atual)
        mm = ManifestManager(get_job_dir(self.tmp, gid))
        man = mm.load_manifest()
        man["voice"]["ref_audio_hash_sha256"] = "0" * 64
        (get_job_dir(self.tmp, gid) / "manifest.json").write_text(json.dumps(man), encoding="utf-8")
        r = self.client.post(f"/generate/{gid}/resume")
        self.assertEqual(r.status_code, 409)
        self.assertIn("referência", r.json()["detail"])
        self.assertEqual(self.gen_row(gid).status, "failed")

    def test_04_retry_sem_corpo_retoma(self):
        gid, m = self._gera_com_falha_no_bloco_3()
        self.engine.calls.clear()
        r = self.client.post(f"/generate/{gid}/retry")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.wait_done(gid).status, "completed")
        self.assertEqual(len(self.engine.calls), m["total_chunks"] - 2)

    def test_05_retry_from_scratch_arquiva_e_recomeca(self):
        gid, m = self._gera_com_falha_no_bloco_3()
        self.engine.calls.clear()
        r = self.client.post(f"/generate/{gid}/retry", json={"from_scratch": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.wait_done(gid).status, "completed")
        self.assertEqual(len(self.engine.calls), m["total_chunks"])
        arquivadas = [p for p in (self.tmp / "jobs").iterdir() if p.name.startswith(f"{gid}_arquivado_")]
        self.assertEqual(len(arquivadas), 1)
        self.assertEqual(json.loads((arquivadas[0] / "manifest.json").read_text(encoding="utf-8"))["job_status"], "falhou")

    def test_06_rotas_de_concluido_recusam_job_incompleto(self):
        gid, _ = self._gera_com_falha_no_bloco_3()
        self.assertEqual(self.client.post(f"/generate/{gid}/reassemble").status_code, 409)
        self.assertEqual(self.client.post(f"/generate/{gid}/blocks/1/regenerate").status_code, 409)
        self.assertEqual(self.client.delete(f"/generate/{gid}/job-files?confirm=true").status_code, 409)
        self.assertTrue(get_job_dir(self.tmp, gid).exists())


# ---------------------------------------------------------------------------
# Segurança
# ---------------------------------------------------------------------------

class TestSeguranca(ApiTestBase):

    def test_01_ids_invalidos(self):
        for bad in ("abc", "..", "nao-e-uuid", str(uuid.uuid4()).upper(), "%2e%2e"):
            for method, url in (
                ("get", f"/generate/{bad}/progress"),
                ("get", f"/generate/{bad}/blocks"),
                ("get", f"/generate/{bad}/blocks/1/audio"),
                ("post", f"/generate/{bad}/resume"),
                ("post", f"/generate/{bad}/reassemble"),
                ("post", f"/generate/{bad}/blocks/1/regenerate"),
                ("delete", f"/generate/{bad}/job-files?confirm=true"),
            ):
                r = getattr(self.client, method)(url)
                self.assertIn(r.status_code, (400, 404), f"{method} {url} -> {r.status_code}")
                self.assertNotEqual(r.status_code, 200)

    def test_02_job_inexistente_404(self):
        gid = str(uuid.uuid4())
        self.assertEqual(self.client.get(f"/generate/{gid}/progress").status_code, 404)
        self.assertEqual(self.client.delete(f"/generate/{gid}/job-files?confirm=true").status_code, 404)

    def test_03_path_traversal_no_manifesto(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        segredo = self.tmp / "segredo.wav"
        sf.write(str(segredo), np.zeros(100, dtype=np.float32), SR)
        mpath = get_job_dir(self.tmp, gid) / "manifest.json"
        man = json.loads(mpath.read_text(encoding="utf-8"))
        man["chunks"][0]["wav_rel_path"] = "../../segredo.wav"
        mpath.write_text(json.dumps(man), encoding="utf-8")
        self.assertEqual(self.client.get(f"/generate/{gid}/blocks/1/audio").status_code, 403)

    def test_04_rotas_mutaveis_recusam_geracao_ativa(self):
        gid = self.generate()
        self.assertEqual(self.wait_done(gid).status, "completed")
        db = self.db()
        db.query(Generation).filter_by(id=gid).update({"status": "generating"})
        db.commit()
        db.close()
        self.assertEqual(self.client.post(f"/generate/{gid}/reassemble").status_code, 409)
        self.assertEqual(self.client.post(f"/generate/{gid}/blocks/1/regenerate").status_code, 409)
        self.assertEqual(self.client.delete(f"/generate/{gid}/job-files?confirm=true").status_code, 409)
        self.assertTrue(get_job_dir(self.tmp, gid).exists())


# ---------------------------------------------------------------------------
# Flags desligadas e reconciliação no startup
# ---------------------------------------------------------------------------

class TestFlagsDesligadas(ApiTestBase):
    flags_on = False

    def test_01_sem_flags_nao_cria_pasta_jobs(self):
        with mock.patch("backend.utils.chunked_tts.generate_chunked") as gc:
            async def _fake(model, text, prompt, **kw):
                return np.zeros(SR, dtype=np.float32), SR
            gc.side_effect = _fake
            gid = self.generate()
            self.assertEqual(self.wait_done(gid).status, "completed")
        self.assertFalse(get_job_dir(self.tmp, gid).exists())
        self.assertEqual(self.client.get(f"/generate/{gid}/progress").status_code, 404)
        self.assertEqual(self.client.post(f"/generate/{gid}/resume").status_code, 404)


class TestReconciliacaoStartup(unittest.TestCase):

    def setUp(self):
        self._old_data_dir = config.get_data_dir()
        self.tmp = Path(tempfile.mkdtemp(prefix="vb_rec_"))
        config.set_data_dir(self.tmp)
        db_session.init_db()

    def tearDown(self):
        db_session.engine.dispose()
        config.set_data_dir(self._old_data_dir)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ordem_reconciliacao_antes_do_update_generico(self):
        from sqlalchemy import text as sa_text

        longo, curto = str(uuid.uuid4()), str(uuid.uuid4())
        db = db_session.SessionLocal()
        db.add(VoiceProfile(id=PROFILE_ID, name="P", voice_type="preset", preset_engine="kokoro"))
        db.add(Generation(id=longo, profile_id=PROFILE_ID, text="x", status="generating"))
        db.add(Generation(id=curto, profile_id=PROFILE_ID, text="y", status="loading_model"))
        db.commit()

        mgr = ManifestManager(get_job_dir(self.tmp, longo))
        mgr.job_dir.mkdir(parents=True)
        mgr.initialize_manifest(longo, [{"idx": 1, "text": "x"}, {"idx": 2, "text": "y"}],
                                {"engine": "kokoro"}, {"profile_id": PROFILE_ID}, "pt", "x y", {"base_seed": 1})
        mgr.update_status(STATUS_EM_ANDAMENTO)

        # Mesma sequência de backend/app.py::_run_startup
        n = long_job_ops.reconcile_long_jobs_on_startup(db, config.get_data_dir())
        db.execute(sa_text(
            "UPDATE generations SET status = 'failed', "
            "error = 'Server was shut down during generation' "
            "WHERE status IN ('generating', 'loading_model')"
        ))
        db.commit()
        db.close()

        self.assertEqual(n, 1)
        db = db_session.SessionLocal()
        g_longo = db.query(Generation).filter_by(id=longo).first()
        g_curto = db.query(Generation).filter_by(id=curto).first()
        self.assertEqual((g_longo.status, g_longo.error), ("failed", "Interrompido: pode ser retomado"))
        self.assertEqual((g_curto.status, g_curto.error), ("failed", "Server was shut down during generation"))
        db.close()
        self.assertEqual(mgr.load_manifest()["job_status"], STATUS_INTERROMPIDO)

    def test_app_py_chama_reconciliacao_antes_do_update(self):
        src = (Path(__file__).resolve().parent.parent / "app.py").read_text(encoding="utf-8")
        self.assertLess(src.index("reconcile_long_jobs_on_startup(db"), src.index("UPDATE generations SET status = 'failed'"))


if __name__ == "__main__":
    unittest.main()

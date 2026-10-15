"""
Excluir do histórico: o X de cada geração e o "Excluir todas" apagam os arquivos do disco.

Motor FALSO, banco e pasta temporários (base de test_long_jobs_api).
Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_excluir_historico.py" -t .
"""

import unittest
import uuid

from backend import config
from backend.database import Generation
from backend.services.long_jobs import get_job_dir
from backend.tests.test_long_jobs_api import ApiTestBase, PROFILE_ID


class ExcluirHistoricoTest(ApiTestBase):
    flags_on = True

    def setUp(self):
        super().setUp()
        import backend.app  # noqa: F401  (routes/history.py importa de backend.app)
        from backend.routes.history import router as history_router
        self.client.app.include_router(history_router)

    def gerar_concluida(self, **kw):
        gid = self.generate(**kw)
        row = self.wait_done(gid)
        self.assertEqual(row.status, "completed", row.error)
        audio = config.resolve_storage_path(row.audio_path)
        self.assertTrue(audio.is_file())
        self.assertTrue(audio.with_suffix(".mp3").is_file(), "save_audio grava uma cópia .mp3")
        return gid, audio

    def test_x_apaga_audio_e_blocos(self):
        gid, audio = self.gerar_concluida()
        job_dir = get_job_dir(self.tmp, gid)
        self.assertTrue(job_dir.is_dir(), "texto longo deveria ter blocos")
        r = self.client.delete(f"/history/{gid}?incluir_blocos=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self.gen_row(gid))
        self.assertFalse(audio.exists(), "o arquivo de áudio deveria ter sido apagado")
        self.assertFalse(audio.with_suffix(".mp3").exists(), "a cópia .mp3 deveria ter sido apagada")
        self.assertFalse(job_dir.exists(), "os blocos deveriam ter sido apagados")

    def test_rota_antiga_sem_parametro_continua_igual(self):
        """Sem incluir_blocos, o DELETE de antes apaga o áudio e mantém os blocos (comportamento antigo)."""
        gid, audio = self.gerar_concluida()
        r = self.client.delete(f"/history/{gid}")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(audio.exists())
        self.assertTrue(get_job_dir(self.tmp, gid).is_dir())

    def test_excluir_todas_exige_confirmacao(self):
        gid, audio = self.gerar_concluida()
        r = self.client.delete("/history/all")
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIsNotNone(self.gen_row(gid))
        self.assertTrue(audio.exists())

    def test_excluir_todas_apaga_tudo_menos_em_andamento(self):
        g1, a1 = self.gerar_concluida()
        g2, a2 = self.gerar_concluida(text="Uma frase curta só.", seed=7)
        ativo = str(uuid.uuid4())
        db = self.db()
        try:
            db.add(Generation(id=ativo, profile_id=PROFILE_ID, text="gerando", language="pt", status="generating"))
            db.commit()
        finally:
            db.close()
        r = self.client.delete("/history/all?confirm=true")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"deleted": 2, "skipped_active": 1})
        for gid, audio in ((g1, a1), (g2, a2)):
            self.assertIsNone(self.gen_row(gid))
            self.assertFalse(audio.exists())
            self.assertFalse(audio.with_suffix(".mp3").exists())
            self.assertFalse(get_job_dir(self.tmp, gid).exists())
        self.assertIsNotNone(self.gen_row(ativo), "geração em andamento não pode ser apagada")

    def test_x_recusa_geracao_em_andamento(self):
        ativo = str(uuid.uuid4())
        db = self.db()
        try:
            db.add(Generation(id=ativo, profile_id=PROFILE_ID, text="gerando", language="pt", status="generating"))
            db.commit()
        finally:
            db.close()
        r = self.client.delete(f"/history/{ativo}?incluir_blocos=true")
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIsNotNone(self.gen_row(ativo))


if __name__ == "__main__":
    unittest.main()

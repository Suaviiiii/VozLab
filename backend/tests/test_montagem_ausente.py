"""Achado 3 da varredura: 'concluido' sem áudio final vira 'interrompido' (retomável) depois do prazo."""
import json
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from backend.services import long_jobs as lj


class TestMontagemAusente(unittest.TestCase):
    def _job(self, master, idade_s):
        data = Path(tempfile.mkdtemp())
        job_id = str(uuid.uuid4())
        d = lj.get_job_dir(data, job_id)
        d.mkdir(parents=True)
        man = {"job_status": lj.STATUS_CONCLUIDO, "master_audio_path": master, "chunks": [],
               "completed_chunks": 2, "total_chunks": 2}
        lj.get_manifest_path(d).write_text(json.dumps(man), encoding="utf-8")
        lj.get_heartbeat_path(d).write_text(json.dumps({"pid": 1, "last_beat": time.time() - idade_s}), encoding="utf-8")
        return data, job_id

    def test_sem_master_e_parado_vira_interrompido(self):
        data, job = self._job(None, lj.MONTAGEM_PRAZO_S + 5)
        st = lj.inspect_job_state(data, job)
        self.assertEqual(st["effective_status"], lj.STATUS_INTERROMPIDO)
        self.assertTrue(st["can_resume"])
        self.assertEqual(st["warning"], "montagem_final_ausente")

    def test_sem_master_mas_recente_continua_concluido(self):
        data, job = self._job(None, 30)
        self.assertEqual(lj.inspect_job_state(data, job)["effective_status"], lj.STATUS_CONCLUIDO)

    def test_com_master_continua_concluido(self):
        data, job = self._job("x.wav", lj.MONTAGEM_PRAZO_S + 5)
        st = lj.inspect_job_state(data, job)
        self.assertEqual(st["effective_status"], lj.STATUS_CONCLUIDO)
        self.assertFalse(st["can_resume"])


if __name__ == "__main__":
    unittest.main()

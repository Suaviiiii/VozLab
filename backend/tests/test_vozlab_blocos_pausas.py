"""
VozLab: tamanho do bloco pela tela e botões de pausa (Curtas/Padrão/Longas).

Motor FALSO, banco e pasta temporários (base de test_long_jobs_api).
Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_vozlab_blocos_pausas.py" -t .
"""

import json
import unittest

from backend.tests.test_long_jobs_api import ApiTestBase, PROFILE_ID
from backend.services.long_jobs import get_job_dir, read_json_with_retry
from backend import config

# Um parágrafo só, ~1.300 caracteres, com frases de ~100 caracteres.
PARAGRAFO = " ".join(
    f"Esta é a frase número {i} de um parágrafo comprido que serve para testar o tamanho dos blocos."
    for i in range(1, 15)
)


class BlocosEPausasTest(ApiTestBase):
    flags_on = True

    def gerar(self, **extra):
        body = {"profile_id": PROFILE_ID, "text": PARAGRAFO, "language": "pt", "engine": "kokoro", "seed": 7}
        body.update(extra)
        r = self.client.post("/generate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        gid = r.json()["id"]
        self.wait_done(gid)
        manifest = read_json_with_retry(get_job_dir(config.get_data_dir(), gid) / "manifest.json")
        return gid, manifest

    def test_tela_sem_tamanho_usa_220_da_configuracao(self):
        """A tela não manda max_chunk_chars: os blocos devem seguir o texto_longo.json (220), não 800."""
        _, m = self.gerar()
        maiores = max(len(c["text"]) for c in m["chunks"])
        self.assertLessEqual(maiores, int(220 * 1.2), f"bloco de {maiores} caracteres")
        self.assertGreaterEqual(len(m["chunks"]), 5)

    def test_tamanho_explicito_continua_valendo(self):
        _, m = self.gerar(max_chunk_chars=600)
        self.assertGreater(max(len(c["text"]) for c in m["chunks"]), 300)

    def test_pausas_escolhidas_ficam_no_trabalho(self):
        _, m = self.gerar(pause_oracao_ms=90, pause_frase_ms=180, pause_paragrafo_ms=450)
        self.assertEqual(m.get("pausas_escolhidas"),
                         {"pause_oracao_ms": 90, "pause_frase_ms": 180, "pause_paragrafo_ms": 450})

    def test_sem_pausas_nao_grava_nada(self):
        _, m = self.gerar()
        self.assertNotIn("pausas_escolhidas", m)

    def test_curtas_encurta_o_audio_final_com_emendas_ligadas(self):
        """Com emendas_v2 ligada (como no VozLab do usuário), Curtas deixa o áudio final mais curto
        que o Padrão, e a diferença bate com as pausas: só a montagem muda, os blocos são idênticos."""
        import soundfile as sf
        cfg_path = config.get_data_dir() / "config" / "texto_longo.json"
        cfg_path.write_text(json.dumps({"texto_longo_v2": True, "persistencia_jobs": True, "emendas_v2": True}),
                            encoding="utf-8")
        gid_p, m_p = self.gerar()
        gid_c, m_c = self.gerar(pause_oracao_ms=90, pause_frase_ms=180, pause_paragrafo_ms=450)
        import numpy as np
        for a, b in zip(m_p["chunks"], m_c["chunks"]):  # amostras decodificadas, nunca hash do WAV
            wa, _ = sf.read(str(get_job_dir(config.get_data_dir(), gid_p) / a["wav_rel_path"]), dtype="float32")
            wb, _ = sf.read(str(get_job_dir(config.get_data_dir(), gid_c) / b["wav_rel_path"]), dtype="float32")
            self.assertTrue(np.array_equal(wa, wb), "os blocos de voz têm que ser idênticos (mesma semente)")
        dur = lambda m: sf.info(str(config.resolve_storage_path(m["master_audio_path"]))).duration  # noqa: E731
        m_p = read_json_with_retry(get_job_dir(config.get_data_dir(), gid_p) / "manifest.json")
        m_c = read_json_with_retry(get_job_dir(config.get_data_dir(), gid_c) / "manifest.json")
        bordas = [c["boundary_type"] for c in m_p["chunks"][:-1]]
        padrao = {"oracao": 140, "frase": 300, "paragrafo": 775}
        curtas = {"oracao": 90, "frase": 180, "paragrafo": 450}
        esperado = sum(padrao[b] - curtas[b] for b in bordas) / 1000
        diferenca = dur(m_p) - dur(m_c)
        print(f"\n[pausas] bordas={bordas} padrão={dur(m_p):.3f}s curtas={dur(m_c):.3f}s "
              f"diferença={diferenca:.3f}s esperado={esperado:.3f}s")
        self.assertAlmostEqual(diferenca, esperado, delta=0.05)

    def test_pausa_fora_da_faixa_recusada(self):
        r = self.client.post("/generate", json={"profile_id": PROFILE_ID, "text": PARAGRAFO, "language": "pt",
                                                "engine": "kokoro", "pause_frase_ms": 99999})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()

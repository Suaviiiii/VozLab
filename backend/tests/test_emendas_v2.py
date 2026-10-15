"""
Testes da emenda v2 (Fase 4) com sinais sintéticos de resposta conhecida
e da rota /reassemble com a flag emendas_v2 ligada.

Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_emendas_v2.py" -t .
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.utils.emendas_v2 import (  # noqa: E402
    assemble_v2,
    envelope_db,
    lookahead_limiter,
    measure_loudness,
    trim_edges,
    true_peak_dbtp,
)

SR = 24000


def fala(seg_s: float, amp: float, seed: int, lead_s: float = 0.0, tail_s: float = 0.0) -> np.ndarray:
    """'Fala' sintética: ruído modulado em sílabas, com silêncio (ruído -70 dBFS) nas pontas."""
    rng = np.random.default_rng(seed)
    n = int(seg_s * SR)
    t = np.arange(n) / SR
    env = 0.55 + 0.45 * np.sin(2 * np.pi * 4.0 * t) ** 2
    core = (rng.standard_normal(n) * env * amp).astype(np.float32)
    floor = lambda s: (rng.standard_normal(int(s * SR)) * 10 ** (-70 / 20)).astype(np.float32)
    return np.concatenate([floor(lead_s), core, floor(tail_s)])


def gap_s(audio: np.ndarray, a: int, b: int) -> float:
    loud = envelope_db(audio, SR) > -45.0
    before = np.flatnonzero(loud[:a])
    after = np.flatnonzero(loud[b:])
    return ((b + after[0]) - before[-1]) / SR


class TestTrim(unittest.TestCase):

    def test_nao_corta_fala_e_mantem_margem(self):
        x = fala(2.0, 0.2, 1, lead_s=0.5, tail_s=0.9)
        y, info = trim_edges(x, SR, -45.0, 40.0)
        self.assertAlmostEqual(info["trim_inicio_s"], 0.46, delta=0.012)
        self.assertAlmostEqual(info["trim_fim_s"], 0.86, delta=0.012)
        env = envelope_db(x, SR)
        a = int(round(info["trim_inicio_s"] * SR))
        b = len(x) - int(round(info["trim_fim_s"] * SR))
        self.assertLess(env[:a].max(), -45.0)
        self.assertLess(env[b:].max(), -45.0)

    def test_idempotente(self):
        x = fala(1.5, 0.3, 2, lead_s=0.3, tail_s=0.6)
        y1, _ = trim_edges(x, SR)
        y2, info2 = trim_edges(y1, SR)
        self.assertLessEqual(abs(len(y1) - len(y2)), 2)
        self.assertLess(info2["trim_inicio_s"] + info2["trim_fim_s"], 0.001)

    def test_bloco_todo_silencio_fica_intacto(self):
        x = (np.random.default_rng(3).standard_normal(SR) * 1e-4).astype(np.float32)
        y, info = trim_edges(x, SR)
        self.assertTrue(info["silencioso"])
        self.assertEqual(len(y), len(x))


class TestLimitador(unittest.TestCase):

    def test_teto_garantido_e_so_atua_nos_picos(self):
        x = fala(3.0, 0.1, 4)
        x[SR] = 0.99
        x[2 * SR] = -0.95
        y, info = lookahead_limiter(x, SR, ceiling_db=-1.5)
        self.assertLessEqual(float(np.max(np.abs(y))), 10 ** (-1.5 / 20) + 1e-6)
        self.assertLess(info["amostras_afetadas_pct"], 5.0)
        far = slice(int(1.5 * SR), int(1.7 * SR))
        self.assertTrue(np.array_equal(y[far], x[far]))


class TestAssembleV2(unittest.TestCase):

    def setUp(self):
        # Volumes bem diferentes (degrau de ~8 dB) e silêncios mortos longos.
        self.blocos = [
            fala(2.0, 0.20, 10, lead_s=0.05, tail_s=0.9),
            fala(2.5, 0.08, 11, lead_s=0.20, tail_s=0.4),
            fala(1.8, 0.25, 12, lead_s=0.10, tail_s=0.8),
            fala(2.2, 0.12, 13, lead_s=0.00, tail_s=0.3),
        ]
        self.fronteiras = ["oracao", "paragrafo", "frase", "frase"]
        self.alvos = {"oracao": 0.14, "frase": 0.30, "paragrafo": 0.775}

    def test_pausas_volume_pico(self):
        audio, rep = assemble_v2(self.blocos, SR, self.fronteiras)
        self.assertTrue(np.all(np.isfinite(audio)))
        pos = rep["posicoes_blocos"]
        for j in range(len(pos) - 1):
            g = gap_s(audio, pos[j][1], pos[j + 1][0])
            self.assertAlmostEqual(g, self.alvos[self.fronteiras[j]], delta=0.04, msg=f"emenda {j + 1}")
        lufs = [measure_loudness(audio[a:b], SR)[0] for a, b in pos]
        self.assertLessEqual(float(np.max(np.abs(np.diff(lufs)))), 1.0)
        self.assertLessEqual(true_peak_dbtp(audio), -1.0 + 0.05)
        self.assertAlmostEqual(measure_loudness(audio, SR)[0], -19.0, delta=1.0)

    def test_pausas_configuraveis(self):
        pz = {"oracao": 300, "frase": 500, "paragrafo": 1200}
        audio, rep = assemble_v2(self.blocos, SR, self.fronteiras, pausas_ms=pz)
        pos = rep["posicoes_blocos"]
        for j in range(len(pos) - 1):
            g = gap_s(audio, pos[j][1], pos[j + 1][0])
            self.assertAlmostEqual(g, pz[self.fronteiras[j]] / 1000, delta=0.04)

    def test_bordas_sem_clique(self):
        audio, rep = assemble_v2(self.blocos, SR, self.fronteiras)
        for a, b in rep["posicoes_blocos"]:
            self.assertLess(abs(float(audio[a])), 1e-3)
            self.assertLess(abs(float(audio[b - 1])), 1e-3)


class TestReassembleComFlag(unittest.TestCase):
    """Rota /reassemble com emendas_v2 ligada (reaproveita a base dos testes de API)."""

    @classmethod
    def setUpClass(cls):
        from backend.tests import test_long_jobs_api as api

        cls.api = api

    def _base(self):
        api = self.api

        class Base(api.ApiTestBase):
            def runTest(self):
                pass

        b = Base()
        b.setUp()
        cfg = b.tmp / "config" / "texto_longo.json"
        cfg.write_text(json.dumps({"texto_longo_v2": True, "persistencia_jobs": True, "emendas_v2": True}),
                       encoding="utf-8")
        return b

    def test_reassemble_aceita_pausas_e_registra(self):
        b = self._base()
        try:
            gid = b.generate()
            self.assertEqual(b.wait_done(gid).status, "completed")
            m = b.manifest(gid)
            self.assertTrue(all(c.get("boundary_type") for c in m["chunks"]))

            r = b.client.post(f"/generate/{gid}/reassemble", json={"pause_paragrafo_ms": 99999})
            self.assertEqual(r.status_code, 422)
            r = b.client.post(f"/generate/{gid}/reassemble", json={"pause_frase_ms": 400, "target_lufs": -18})
            self.assertEqual(r.status_code, 200, r.text)
            m = b.manifest(gid)
            self.assertEqual(m["last_assembly_settings"]["pause_frase_ms"], 400)
            self.assertEqual(m["last_assembly_settings"]["target_lufs"], -18)
            import soundfile as sf
            from backend import config

            audio, sr = sf.read(str(config.resolve_storage_path(b.gen_row(gid).audio_path)), dtype="float32")
            self.assertAlmostEqual(measure_loudness(audio, sr)[0], -18.0, delta=1.0)
        finally:
            b.tearDown()


if __name__ == "__main__":
    unittest.main()

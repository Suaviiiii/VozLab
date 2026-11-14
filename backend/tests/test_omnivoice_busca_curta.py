"""Busca curta do MIOpen no trabalhador do OmniVoice: só na AMD com 7,5 GB ou mais, sem atropelar quem já definiu.

    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_omnivoice_busca_curta.py" -t .
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "OmniVoice"))
import omnivoice_trabalhador as t  # noqa: E402


class TestBuscaCurta(unittest.TestCase):
    def test_liga_na_amd_de_8gb(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(t.busca_curta(True, 7.98))
            self.assertEqual(os.environ["MIOPEN_FIND_MODE"], "FAST")

    def test_fica_na_busca_longa(self):
        for amd, vram, env in ((True, 4.0, {}), (False, 12.0, {}), (True, 8.0, {"OMNIVOICE_BUSCA_LONGA": "1"})):
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertFalse(t.busca_curta(amd, vram))
                self.assertNotIn("MIOPEN_FIND_MODE", os.environ)

    def test_nao_atropela_valor_existente(self):
        with mock.patch.dict(os.environ, {"MIOPEN_FIND_MODE": "NORMAL"}, clear=True):
            t.busca_curta(True, 8.0)
            self.assertEqual(os.environ["MIOPEN_FIND_MODE"], "NORMAL")


if __name__ == "__main__":
    unittest.main()

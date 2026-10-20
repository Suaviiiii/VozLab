"""Um arranque do serviço OmniVoice por vez: pedidos seguidos não abrem o .bat de novo.

    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_omnivoice_arranque.py" -t .
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.backends import omnivoice_backend as ob


class TestArranqueUnico(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        bat = Path(self.tmp.name) / "iniciar_omnivoice_service.bat"
        bat.write_text("@echo off\n", encoding="ascii")
        self.patches = [
            mock.patch.object(ob, "OMNIVOICE_LAUNCHER_PATH", bat),
            mock.patch.object(ob.arranque, "ARRANQUE_MAX_S", 1.0),  # o serviço "nunca sobe": cada espera dura ~1 s
            mock.patch.object(ob.arranque, "_arranque_desde", -1e9),
            mock.patch.object(ob.OmniVoiceBackend, "_escutando", return_value=False),
            mock.patch("backend.config.get_data_dir", return_value=self.tmp.name),
        ]
        for p in self.patches:
            p.start()
        self.popen = mock.patch.object(ob.subprocess, "Popen").start()

    def tearDown(self):
        mock.patch.stopall()
        self.tmp.cleanup()

    def test_dois_pedidos_ao_mesmo_tempo_abrem_um_servico(self):
        async def dois():
            return await asyncio.gather(ob.OmniVoiceBackend()._ligar_servico(),
                                        ob.OmniVoiceBackend()._ligar_servico(), return_exceptions=True)

        resultados = asyncio.run(dois())
        self.assertEqual(self.popen.call_count, 1)
        for r in resultados:  # nenhum subiu: os dois avisam, com o tempo de espera certo
            self.assertIsInstance(r, RuntimeError)
            self.assertIn("1 s", str(r))

    def test_depois_do_prazo_pode_tentar_de_novo(self):
        ob.arranque.marcar_arranque()
        self.assertTrue(ob.arranque.arranque_em_andamento())
        with mock.patch.object(ob.arranque, "_arranque_desde", ob.time.monotonic() - 5.0):
            self.assertFalse(ob.arranque.arranque_em_andamento())


if __name__ == "__main__":
    unittest.main()

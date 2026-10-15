"""
A ação 'start' de /omnivoice/control não pode travar o servidor enquanto espera
o serviço subir (antes: time.sleep em rota assíncrona, até 4 s de trava).

Não inicia o OmniVoice: o Popen e a checagem de saúde são simulados.

Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_omnivoice_control_nao_bloqueia.py" -t .
"""

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.routes import omnivoice  # noqa: E402


class TestControlStartNaoBloqueia(unittest.IsolatedAsyncioTestCase):

    async def test_laco_continua_livre_durante_a_espera(self):
        respostas = iter([(False, None), (False, None), (False, None), (False, None), (True, None)])

        async def fake_health():
            await asyncio.sleep(0)  # como o httpx real: devolve o controle ao laço
            return next(respostas)

        with tempfile.TemporaryDirectory() as tmp:
            launcher = Path(tmp) / "iniciar_omnivoice_service.bat"
            launcher.write_text("rem teste", encoding="utf-8")
            with mock.patch.object(omnivoice, "_check_service_health", fake_health), \
                 mock.patch.object(omnivoice, "OMNIVOICE_LAUNCHER_PATH", launcher), \
                 mock.patch.object(omnivoice.subprocess, "Popen") as popen:

                maior_atraso = 0.0
                parar = False

                async def ticker():
                    nonlocal maior_atraso
                    while not parar:
                        t0 = time.perf_counter()
                        await asyncio.sleep(0.02)
                        maior_atraso = max(maior_atraso, time.perf_counter() - t0 - 0.02)

                tarefa = asyncio.create_task(ticker())
                await asyncio.sleep(0.05)  # medidor já rodando antes do clique
                t0 = time.perf_counter()
                resultado = await omnivoice.control_service(omnivoice.OmniVoiceControlRequest(action="start"))
                duracao = time.perf_counter() - t0
                parar = True
                await tarefa

        self.assertEqual(resultado["status"], "started")
        self.assertEqual(popen.call_count, 1)
        self.assertGreaterEqual(duracao, 1.5)  # esperou de verdade (4 checagens de 0,5 s)
        self.assertLess(maior_atraso, 0.2, f"laço travou por {maior_atraso:.3f}s")
        print(f"\n[omnivoice start] esperou {duracao:.2f}s; maior atraso do laço {maior_atraso * 1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()

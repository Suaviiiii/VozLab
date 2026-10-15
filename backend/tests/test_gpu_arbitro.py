"""Testes do árbitro da GPU entre o VozLab e o OmniVoice (Missão OmniVoice, OV-2). Sem GPU e sem rede."""

import asyncio
import unittest
from unittest import mock

from backend.services import gpu_arbitro, task_queue


def rodar(coro):
    return asyncio.run(coro)


class PedirPlaca(unittest.TestCase):
    def setUp(self):
        self._intervalo = gpu_arbitro.INTERVALO_S
        gpu_arbitro.INTERVALO_S = 0.01

    def tearDown(self):
        gpu_arbitro.INTERVALO_S = self._intervalo

    def _respostas(self, *seq):
        it = iter(seq)

        async def falso():
            return dict(next(it))

        return mock.patch.object(gpu_arbitro, "_pedir_ao_servico", side_effect=falso)

    def test_servico_desligado_segue_na_hora(self):
        with self._respostas({"resultado": "desligado"}) as m:
            r = rodar(gpu_arbitro.pedir_placa("g1"))
        self.assertEqual(r["resultado"], "desligado")
        self.assertEqual(m.call_count, 1)

    def test_espera_enquanto_ocupado_e_segue_quando_cede(self):
        with self._respostas({"resultado": "ocupado"}, {"resultado": "ocupado"}, {"resultado": "cedido"}) as m:
            r = rodar(gpu_arbitro.pedir_placa("g2"))
        self.assertEqual(r["resultado"], "cedido")
        self.assertEqual(m.call_count, 3)
        self.assertIsNone(gpu_arbitro.estado()["aguardando_omnivoice"])

    def test_desiste_se_cancelada_durante_a_espera(self):
        cancelada = {"v": False}

        async def ocupado_e_cancela():
            cancelada["v"] = True
            return {"resultado": "ocupado"}

        with mock.patch.object(gpu_arbitro, "_pedir_ao_servico", side_effect=ocupado_e_cancela):
            r = rodar(gpu_arbitro.pedir_placa("g3", desistir=lambda: cancelada["v"]))
        self.assertEqual(r["resultado"], "desistiu")

    def test_servico_antigo_sem_rota_nao_bloqueia(self):
        with self._respostas({"resultado": "sem_arbitro"}):
            r = rodar(gpu_arbitro.pedir_placa("g4"))
        self.assertEqual(r["resultado"], "sem_arbitro")


class CederPlaca(unittest.TestCase):
    def test_ocupado_quando_vozlab_gera(self):
        with mock.patch.object(gpu_arbitro, "vozlab_ocupado", return_value=True), \
                mock.patch.object(gpu_arbitro, "descarregar_modelos_de_voz") as desc:
            r = rodar(gpu_arbitro.ceder_placa("omnivoice"))
        self.assertEqual(r["resultado"], "ocupado")
        desc.assert_not_called()  # nunca descarrega no meio de uma geração

    def test_cede_e_descarrega_quando_parado(self):
        with mock.patch.object(gpu_arbitro, "vozlab_ocupado", return_value=False), \
                mock.patch.object(gpu_arbitro, "descarregar_modelos_de_voz", return_value=["qwen-tts-1.7B"]):
            r = rodar(gpu_arbitro.ceder_placa("omnivoice"))
        self.assertEqual(r, {"resultado": "cedido", "descarregados": ["qwen-tts-1.7B"]})

    def test_livre_quando_nada_carregado(self):
        with mock.patch.object(gpu_arbitro, "vozlab_ocupado", return_value=False), \
                mock.patch.object(gpu_arbitro, "descarregar_modelos_de_voz", return_value=[]):
            r = rodar(gpu_arbitro.ceder_placa())
        self.assertEqual(r["resultado"], "livre")

    def test_vozlab_ocupado_le_a_fila(self):
        async def cenario():
            ev = asyncio.Event()
            t = asyncio.create_task(ev.wait())
            task_queue._running_generation_tasks["teste_arbitro"] = t
            try:
                ocupado = gpu_arbitro.vozlab_ocupado()
            finally:
                ev.set()
                await t
                task_queue._running_generation_tasks.pop("teste_arbitro", None)
            return ocupado, gpu_arbitro.vozlab_ocupado()

        durante, depois = rodar(cenario())
        self.assertTrue(durante)
        self.assertFalse(depois)


if __name__ == "__main__":
    unittest.main()

"""Testes do perfil de placa (Missão GPU por famílias, GPU 2). Funções puras: não usam a placa."""

import unittest

from backend.services import perfil_placa as p


def sit(perfil, modelo):
    return perfil["modelos"][modelo]["situacao"]


class TestFamilia(unittest.TestCase):
    def test_nomes_amd(self):
        self.assertEqual(p.familia_por_nome("AMD Radeon RX 7600"), "amd_rx7000")
        self.assertEqual(p.familia_por_nome("AMD Radeon RX 6600 XT"), "amd_rx6000")
        self.assertEqual(p.familia_por_nome("AMD Radeon RX 6500 XT"), "amd_rx6000")
        self.assertEqual(p.familia_por_nome("AMD Radeon RX 9060 XT"), "amd_rx9000")
        self.assertEqual(p.familia_por_nome("Radeon RX 580 Series", fabricante="amd"), "amd_outra")

    def test_nome_vence_o_disfarce_do_gfx(self):
        # Com HSA_OVERRIDE_GFX_VERSION=11.0.0, uma RX 6600 pode informar gfx1100: o nome decide.
        self.assertEqual(p.familia_por_nome("AMD Radeon RX 6600", arch="gfx1100"), "amd_rx6000")

    def test_arquitetura_sem_nome(self):
        self.assertEqual(p.familia_por_nome("", arch="gfx1102"), "amd_rx7000")
        self.assertEqual(p.familia_por_nome("", arch="gfx1032"), "amd_rx6000")
        self.assertEqual(p.familia_por_nome("", capacidade=(6, 1)), "nvidia_gtx1000")
        self.assertEqual(p.familia_por_nome("", capacidade=(8, 6)), "nvidia_rtx3000")

    def test_nomes_nvidia(self):
        self.assertEqual(p.familia_por_nome("NVIDIA GeForce GTX 1050 Ti"), "nvidia_gtx1000")
        self.assertEqual(p.familia_por_nome("NVIDIA GeForce RTX 3060"), "nvidia_rtx3000")
        self.assertEqual(p.familia_por_nome("NVIDIA GeForce RTX 4060"), "nvidia_rtx4000")
        self.assertEqual(p.familia_por_nome("NVIDIA GeForce GTX 1650"), "nvidia_outra")

    def test_classe_vram(self):
        self.assertEqual(p.classe_vram(8176), 8)
        self.assertEqual(p.classe_vram(16368), 8)
        self.assertEqual(p.classe_vram(6144), 6)
        self.assertEqual(p.classe_vram(4096), 4)
        self.assertIsNone(p.classe_vram(None))


class TestSituacao(unittest.TestCase):
    def test_rx7600_tudo_ok_como_hoje(self):
        perfil = p.montar_perfil("AMD Radeon RX 7600", "amd", "gfx1102", None, 8176)
        self.assertEqual(perfil["familia"], "amd_rx7000")
        self.assertEqual(perfil["avisos"], [])
        for m in p.PRINCIPAIS:
            self.assertEqual(sit(perfil, m), p.OK, m)
        self.assertEqual(sit(perfil, "qwen3-4b"), p.NAO_UTILIZAVEL)

    def test_6gb_17b_com_aviso(self):
        perfil = p.montar_perfil("NVIDIA GeForce RTX 3050", "nvidia", "", (8, 6), 6144)
        self.assertEqual(sit(perfil, "qwen-tts-1.7B"), p.AVISO)
        self.assertEqual(sit(perfil, "qwen-custom-voice-1.7B"), p.AVISO)
        for m in ("qwen-tts-0.6B", "qwen-custom-voice-0.6B", "chatterbox-tts", "omnivoice"):
            self.assertEqual(sit(perfil, m), p.OK, m)
        self.assertTrue(any("não testado" in a for a in perfil["avisos"]))

    def test_4gb_amd(self):
        perfil = p.montar_perfil("AMD Radeon RX 6500 XT", "amd", "gfx1034", None, 4080)
        for m in ("qwen-tts-1.7B", "qwen-tts-0.6B", "qwen-custom-voice-1.7B", "chatterbox-tts"):
            self.assertEqual(sit(perfil, m), p.NAO_UTILIZAVEL, m)
        self.assertEqual(sit(perfil, "qwen-custom-voice-0.6B"), p.AVISO)
        self.assertEqual(sit(perfil, "omnivoice"), p.AVISO)

    def test_gtx1000_so_omnivoice(self):
        perfil = p.montar_perfil("NVIDIA GeForce GTX 1050 Ti", "nvidia", "", (6, 1), 4096)
        self.assertEqual(sit(perfil, "omnivoice"), p.NAO_TESTADO)
        for m in p.PRINCIPAIS + p.SECUNDARIOS:
            if m != "omnivoice":
                self.assertEqual(sit(perfil, m), p.NAO_UTILIZAVEL, m)

    def test_sem_placa_avisa(self):
        perfil = p.montar_perfil("", "")
        self.assertEqual(perfil["familia"], "sem_placa")
        self.assertTrue(perfil["avisos"] and "placa de vídeo" in perfil["avisos"][0])
        self.assertTrue(all(v["situacao"] == p.NAO_UTILIZAVEL for v in perfil["modelos"].values()))

    def test_fora_do_escopo_avisa(self):
        perfil = p.montar_perfil("NVIDIA GeForce RTX 4060", "nvidia", "", (8, 9), 8188)
        self.assertFalse(perfil["no_escopo"])
        self.assertTrue(any("fora das famílias" in a for a in perfil["avisos"]))


if __name__ == "__main__":
    unittest.main()

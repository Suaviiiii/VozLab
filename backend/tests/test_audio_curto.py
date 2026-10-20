"""Áudio curto demais para o texto: gera de novo com outra semente; desiste com mensagem clara.

    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_audio_curto.py" -t .
"""
import asyncio
import unittest

import numpy as np

from backend.utils.chunked_tts import CURTO_TENTATIVAS, audio_curto_demais, generate_single_chunk

TEXTO = "fazendo um teste da geração de voz com esse modelo agora no voz lab, e vendo se sai inteiro desta vez."


class BackendFalso:
    def __init__(self, duracoes):
        self.duracoes = list(duracoes)
        self.sementes = []

    async def generate(self, text, voice_prompt, language, seed, instruct):
        self.sementes.append(seed)
        return np.zeros(int(24000 * self.duracoes.pop(0)), dtype=np.float32), 24000


def gerar(backend, semente=42):
    return asyncio.run(generate_single_chunk(backend=backend, chunk_text=TEXTO, voice_prompt={},
                                             language="pt", chunk_seed=semente))


class TestAudioCurto(unittest.TestCase):
    def test_limite(self):
        self.assertTrue(audio_curto_demais(113, 0.16))
        self.assertFalse(audio_curto_demais(113, 7.8))   # voz brasileira, a mais curta medida
        self.assertFalse(audio_curto_demais(10, 0.1))    # texto curtinho não é conferido

    def test_normal_gera_uma_vez(self):
        b = BackendFalso([8.0])
        audio, sr = gerar(b)
        self.assertEqual(b.sementes, [42])
        self.assertAlmostEqual(len(audio) / sr, 8.0)

    def test_curto_gera_de_novo_com_outra_semente(self):
        b = BackendFalso([0.16, 0.24, 9.0])
        audio, sr = gerar(b)
        self.assertEqual(b.sementes, [42, 42 + 7919, 42 + 2 * 7919])
        self.assertAlmostEqual(len(audio) / sr, 9.0)

    def test_sem_semente_continua_sem_semente(self):
        b = BackendFalso([0.16, 9.0])
        gerar(b, semente=None)
        self.assertEqual(b.sementes, [None, None])

    def test_sempre_curto_falha_com_mensagem(self):
        b = BackendFalso([0.16] * CURTO_TENTATIVAS)
        with self.assertRaises(RuntimeError) as ctx:
            gerar(b)
        self.assertIn("curto demais", str(ctx.exception))
        self.assertEqual(len(b.sementes), CURTO_TENTATIVAS)


if __name__ == "__main__":
    unittest.main()

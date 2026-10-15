"""Testes do motor OmniVoice do VozLab (Missão OmniVoice, OV-4). Sem GPU e sem o serviço: HTTP simulado."""

import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import soundfile as sf

from backend.backends import omnivoice_backend as ob
from backend.backends import TTSBackend, get_tts_backend_for_engine
from backend.services import gpu_arbitro


def wav_bytes(seg=1.0, sr=24000):
    buf = io.BytesIO()
    sf.write(buf, np.zeros(int(seg * sr), dtype=np.float32), sr, format="WAV", subtype="FLOAT")
    return buf.getvalue()


class Resposta:
    def __init__(self, status=200, conteudo=b"", js=None, headers=None):
        self.status_code, self.content, self._js, self.headers, self.text = status, conteudo, js, headers or {}, ""

    def json(self):
        return self._js


class ClienteFalso:
    """Substitui httpx.AsyncClient: guarda o último pedido e devolve a resposta escolhida."""
    ultimo = None
    resposta = None

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        ClienteFalso.ultimo = (url, json)
        return ClienteFalso.resposta

    async def get(self, url):
        return Resposta(200, js={"versao_servico": "ov3"})


class Registro(unittest.TestCase):
    def test_motor_registrado_e_cumpre_a_interface(self):
        b = get_tts_backend_for_engine("omnivoice")
        self.assertIsInstance(b, ob.OmniVoiceBackend)
        self.assertIsInstance(b, TTSBackend)

    def test_omnivoice_e_motor_remoto_no_arbitro(self):
        self.assertIn("omnivoice", gpu_arbitro.MOTORES_REMOTOS)
        gpu_arbitro._motor_por_geracao["g_ov"] = "omnivoice"
        gpu_arbitro._motor_por_geracao["g_qw"] = "qwen"
        self.assertTrue(gpu_arbitro.geracao_remota("g_ov"))
        self.assertFalse(gpu_arbitro.geracao_remota("g_qw"))

    def test_pedido_de_geracao_aceita_omnivoice(self):
        from backend.models import GenerationRequest
        r = GenerationRequest(profile_id="x", text="oi", language="pt", engine="omnivoice")
        self.assertEqual(r.engine, "omnivoice")


class Gerar(unittest.TestCase):
    def setUp(self):
        self.b = ob.OmniVoiceBackend()
        self.b._pronto = True

    def _gerar(self, prompt, seed=42):
        with mock.patch.object(ob.httpx, "AsyncClient", ClienteFalso), \
                mock.patch.object(ob, "ler_ajustes", return_value={"num_step": 16, "guidance_scale": 2.0}):
            return asyncio.run(self.b.generate("Olá.", prompt, language="pt", seed=seed))

    def test_clonar_envia_semente_sem_divisao_e_devolve_audio(self):
        ClienteFalso.resposta = Resposta(200, wav_bytes(1.5), headers={"x-segundos-carga": "0"})
        audio, sr = self._gerar({"ref_audio": "C:/ref.wav", "ref_text": "texto"})
        url, corpo = ClienteFalso.ultimo
        self.assertTrue(url.endswith("/v2/gerar_bloco"))
        self.assertEqual(corpo["mode"], "clone")
        self.assertEqual(corpo["seed"], 42)
        self.assertFalse(corpo["divisao_interna"])
        self.assertEqual(corpo["ref_audio_path"], "C:/ref.wav")
        self.assertEqual(sr, 24000)
        self.assertEqual(len(audio), 36000)

    def test_voz_desenhada_vira_modo_design(self):
        ClienteFalso.resposta = Resposta(200, wav_bytes(0.5))
        self._gerar({"voice_type": "designed", "design_prompt": "female, young adult"})
        self.assertEqual(ClienteFalso.ultimo[1]["mode"], "design")
        self.assertEqual(ClienteFalso.ultimo[1]["instruct"], "female, young adult")

    def test_cancelado_no_servico_vira_cancelamento(self):
        ClienteFalso.resposta = Resposta(409, b"")
        with self.assertRaises(asyncio.CancelledError):
            self._gerar({"ref_audio": "C:/ref.wav", "ref_text": "t"})

    def test_erro_do_servico_vira_erro_legivel(self):
        ClienteFalso.resposta = Resposta(500, b"")
        with self.assertRaises(RuntimeError):
            self._gerar({"ref_audio": "C:/ref.wav", "ref_text": "t"})

    def test_sem_voz_clonada_nao_gera(self):
        with self.assertRaises(ValueError):
            self._gerar({"voice_type": "preset", "preset_engine": "kokoro"})


class Referencia(unittest.TestCase):
    def test_referencia_curta_fica_como_esta(self):
        with tempfile.TemporaryDirectory() as d:
            arq = Path(d) / "ref.wav"
            sf.write(str(arq), np.zeros(24000 * 8, dtype=np.float32), 24000)
            prompt, _ = asyncio.run(ob.OmniVoiceBackend().create_voice_prompt(str(arq), "texto da referência"))
        self.assertEqual(prompt["ref_audio"], str(arq))
        self.assertEqual(prompt["ref_text"], "texto da referência")

    def test_referencia_longa_vira_copia_de_ate_10s_e_original_intacta(self):
        with tempfile.TemporaryDirectory() as d:
            arq = Path(d) / "longa.wav"
            onda = (np.random.default_rng(0).standard_normal(24000 * 21) * 0.1).astype(np.float32)
            onda[int(9.0 * 24000):int(9.3 * 24000)] = 0.0  # uma pausa aos 9 s
            sf.write(str(arq), onda, 24000)
            antes = arq.read_bytes()
            with mock.patch("backend.config.get_cache_dir", return_value=Path(d) / "cache"):
                prompt, _ = asyncio.run(ob.OmniVoiceBackend().create_voice_prompt(str(arq), "texto inteiro"))
            copia = Path(prompt["ref_audio"])
            dur = sf.info(str(copia)).duration
            self.assertNotEqual(copia, arq)
            self.assertTrue(8.0 <= dur <= 10.0, dur)
            self.assertTrue(9.0 <= dur <= 9.35, f"cortou fora da pausa: {dur}")
            self.assertIsNone(prompt["ref_text"])  # o texto inteiro não bate com o áudio cortado
            self.assertEqual(arq.read_bytes(), antes)  # a original não foi tocada


class Ajustes(unittest.TestCase):
    def test_gravar_e_ler_com_limites(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("backend.config.get_data_dir", return_value=Path(d)):
                self.assertEqual(ob.ler_ajustes(), ob.AJUSTES_PADRAO)
                ob.gravar_ajustes(200, 9.0)
                self.assertEqual(ob.ler_ajustes(), {"num_step": 64, "guidance_scale": 4.0})
                self.assertEqual(json.loads((Path(d) / "config" / "omnivoice.json").read_text(encoding="utf-8"))["num_step"], 64)


if __name__ == "__main__":
    unittest.main()

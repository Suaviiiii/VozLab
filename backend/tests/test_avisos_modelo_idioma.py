"""
Fase 8: confere o arquivo de avisos por modelo e idioma da interface nova.

Só leitura de arquivos (sem servidor, sem GPU).
Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_avisos_modelo_idioma.py" -t .
"""

import json
import re
import unittest
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
NOVO = RAIZ / "frontend" / "novo"
ARQUIVO = NOVO / "avisos_modelo_idioma.json"


def opcoes(select_id: str) -> set:
    html = (NOVO / "index.html").read_text(encoding="utf-8")
    bloco = re.search(r'<select id="' + select_id + r'"[^>]*>(.*?)</select>', html, re.S)
    return set(re.findall(r'<option value="([^"]*)"', bloco.group(1)))


class AvisosModeloIdiomaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dados = json.loads(ARQUIVO.read_text(encoding="utf-8"))

    def test_formato(self):
        self.assertEqual(self.dados["kind"], "avisos_modelo_idioma")
        self.assertTrue(self.dados["avisos"])
        ids = [a["id"] for a in self.dados["avisos"]]
        self.assertEqual(len(ids), len(set(ids)), "id repetido")

    def test_modelos_e_idiomas_existem_na_tela(self):
        modelos = opcoes("gen-model")
        idiomas = opcoes("gen-language") - {"auto"}
        for a in self.dados["avisos"]:
            with self.subTest(aviso=a["id"]):
                self.assertTrue(a["modelos"])
                self.assertTrue(set(a["modelos"]) <= modelos, a["modelos"])
                self.assertTrue(a["idiomas"])
                self.assertTrue(set(a["idiomas"]) <= idiomas, a["idiomas"])

    def test_todo_aviso_tem_medicao_registrada(self):
        """Regra da Fase 8: só dados medidos. Cada aviso aponta um relatório que existe e tem data."""
        for a in self.dados["avisos"]:
            with self.subTest(aviso=a["id"]):
                self.assertTrue((RAIZ / a["fonte"]).is_file(), a["fonte"])
                self.assertRegex(a["medido_em"], r"^\d{4}-\d{2}-\d{2}$")

    def test_numeros_batem_com_a_fonte(self):
        f5 = (RAIZ / "relatorios" / "teste_texto_longo.md").read_text(encoding="utf-8")
        self.assertIn("68.33%", f5)
        fase7 = (RAIZ / "relatorios" / "texto_longo" / "fase7.md").read_text(encoding="utf-8")
        self.assertIn("PT-PT", fase7)
        self.assertIn("44 s", fase7)

    def test_avisos_curtos(self):
        """Interface limpa: a linha visível é curta; o detalhe fica na dica do mouse."""
        for a in self.dados["avisos"]:
            with self.subTest(aviso=a["id"]):
                self.assertLessEqual(len(a["aviso"]), 120, a["aviso"])

    def test_tela_nao_bloqueia(self):
        """O aviso só mostra texto: a função não mexe no botão Gerar."""
        js = (NOVO / "app.js").read_text(encoding="utf-8")
        corpo = js[js.index("function updateModelWarning"):js.index("async function loadModelWarnings")]
        self.assertNotIn("generate-button", corpo)
        self.assertNotIn("disabled", corpo)


if __name__ == "__main__":
    unittest.main()

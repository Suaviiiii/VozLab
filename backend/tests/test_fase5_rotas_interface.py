"""
Fase 5 (D3) e VozLab: todo endereço de página abre a interface nova (a clássica foi aposentada).

Uso:
    venv_rocm\\Scripts\\python.exe -m unittest discover -s backend\\tests -p "test_fase5_rotas_interface.py" -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from backend.app import _mount_frontend  # noqa: E402

FRONT = Path(__file__).resolve().parent.parent.parent / "frontend"


class RotaNovoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = FastAPI()
        _mount_frontend(app)
        cls.client = TestClient(app)
        cls.nova = (FRONT / "novo" / "index.html").read_bytes()

    def test_novo_com_barra_abre_interface_nova(self):
        r = self.client.get("/novo/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, self.nova)

    def test_novo_index_continua_igual(self):
        self.assertEqual(self.client.get("/novo/index.html").content, self.nova)

    def test_classico_aposentado_abre_a_nova(self):
        for path in ("/classico", "/classico/", "/classico/index.html"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.content, self.nova)

    def test_raiz_abre_interface_nova(self):
        """VozLab: a rota "/" (health.root) entrega a interface nova."""
        from backend.routes import health
        app = FastAPI()
        app.include_router(health.router)
        _mount_frontend(app)
        r = TestClient(app).get("/")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, self.nova)

    def test_enderecos_antigos_abrem_a_nova(self):
        for path in ("/voices", "/stories", "/rota/inexistente"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.content, self.nova)

    def test_arquivos_da_interface_nova_sao_servidos(self):
        for name in ("app.js", "texto-longo.js", "texto-longo.css", "importar-vozes.js"):
            with self.subTest(arquivo=name):
                r = self.client.get("/novo/" + name)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.content, (FRONT / "novo" / name).read_bytes())

    def test_travessia_de_pasta_nao_sai_do_frontend(self):
        r = self.client.get("/novo/..%2F..%2Fbackend%2Fapp.py")
        self.assertNotIn(b"_mount_frontend", r.content)


if __name__ == "__main__":
    unittest.main()

"""Um arranque do serviço OmniVoice por vez.

Em 16/10/2026 o serviço levou mais de 60 s para subir, e o botão "Ligar" e a geração do painel abriram o
.bat de novo várias vezes (5 aberturas no registro), seguidas de quedas do trabalhador de GPU. Enquanto um
arranque estiver em andamento, ninguém abre outro: só espera. Arquivo leve (sem torch) para a rota usar.
"""
import time

ARRANQUE_MAX_S = 180.0
_arranque_desde = -ARRANQUE_MAX_S


def arranque_em_andamento() -> bool:
    return time.monotonic() - _arranque_desde < ARRANQUE_MAX_S


def marcar_arranque() -> None:
    global _arranque_desde
    _arranque_desde = time.monotonic()

"""Decisões da trava de acesso remoto (H1), isoladas das dependências pesadas.

A trava de origem em ``app.py`` usa estas funções. Ficam aqui, sem importar
torch/FastAPI, para poderem ser testadas de forma rápida e direta.

Regra: pedidos da própria máquina (loopback) sempre passam — é a tela do VozLab.
Pedidos de outra máquina só valem com ``Authorization: Bearer <VOICEBOX_AUTH_TOKEN>``;
sem o token configurado, nada de fora entra.
"""

from __future__ import annotations

import ipaddress
import secrets


def eh_loopback(remote_addr: str | None) -> bool:
    """True quando o endereço de origem é da interface local (127.0.0.0/8, ::1).

    Endereço ausente ou não-parseável (ex.: o 'testclient' do TestClient) conta
    como NÃO-loopback — quem decide liberar trata isso como 'exigir token'.
    """
    if not remote_addr:
        return False
    try:
        return ipaddress.ip_address(remote_addr).is_loopback
    except ValueError:
        return False


def acesso_remoto_liberado(
    remote_addr: str | None, authorization_header: str | None, auth_token: str
) -> bool:
    """True se o pedido pode prosseguir pela regra de acesso remoto.

    - Loopback: sempre liberado (sem token).
    - Fora do loopback: exige token configurado E ``Bearer`` igual a ele
      (comparado em tempo constante).
    """
    if eh_loopback(remote_addr):
        return True
    if not auth_token:
        return False
    enviado = authorization_header or ""
    prefixo = "bearer "
    token = enviado[len(prefixo):].strip() if enviado.lower().startswith(prefixo) else ""
    return bool(token and secrets.compare_digest(token, auth_token))

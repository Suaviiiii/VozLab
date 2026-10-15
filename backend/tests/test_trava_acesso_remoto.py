"""Testes da trava de acesso remoto (H1).

Garante que o uso local (loopback) nunca precisa de token e que qualquer
cliente de outra máquina só passa com o Bearer correto.
"""

from backend.utils.acesso import acesso_remoto_liberado, eh_loopback


def test_loopback_e_reconhecido():
    assert eh_loopback("127.0.0.1") is True
    assert eh_loopback("::1") is True
    assert eh_loopback("127.5.5.5") is True  # todo o 127.0.0.0/8


def test_nao_loopback_e_host_invalido():
    assert eh_loopback("192.168.0.10") is False
    assert eh_loopback("testclient") is False  # host do TestClient não é IP
    assert eh_loopback(None) is False
    assert eh_loopback("") is False


def test_loopback_passa_sem_token():
    # A tela do VozLab roda em 127.0.0.1 e nunca precisa de token.
    assert acesso_remoto_liberado("127.0.0.1", None, "") is True
    assert acesso_remoto_liberado("::1", None, "qualquer") is True


def test_remoto_sem_token_configurado_e_bloqueado():
    # Sem VOICEBOX_AUTH_TOKEN, acesso remoto fica fechado.
    assert acesso_remoto_liberado("192.168.0.10", "Bearer x", "") is False
    assert acesso_remoto_liberado("10.0.0.5", None, "") is False


def test_remoto_com_token_correto_passa():
    assert acesso_remoto_liberado("192.168.0.10", "Bearer segredo", "segredo") is True
    assert acesso_remoto_liberado("192.168.0.10", "bearer segredo", "segredo") is True  # case-insensitive


def test_remoto_com_token_errado_ou_ausente_e_bloqueado():
    assert acesso_remoto_liberado("192.168.0.10", "Bearer errado", "segredo") is False
    assert acesso_remoto_liberado("192.168.0.10", None, "segredo") is False
    assert acesso_remoto_liberado("192.168.0.10", "segredo", "segredo") is False  # sem o prefixo Bearer

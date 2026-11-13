import os
import sys
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from rate_limiter import RateLimiterMemoria


@pytest.fixture
def limiter():
    """Fixture que provee un limitador con umbral de 5 peticiones y ventana de 2 segundos."""
    return RateLimiterMemoria(max_peticiones=5, ventana_segundos=2)


def test_permite_hasta_limite_maximo(limiter):
    ip = "192.168.1.100"
    for i in range(5):
        permitido, retry_after = limiter.verificar_limite(ip)
        assert permitido, f"La petición {i+1} debería estar permitida"
        assert retry_after == 0


def test_bloquea_peticion_que_supera_limite(limiter):
    ip = "192.168.1.101"
    for _ in range(5):
        limiter.verificar_limite(ip)

    # La 6ta petición debe ser rechazada
    permitido, retry_after = limiter.verificar_limite(ip)
    assert not permitido, "La petición 6 debería ser bloqueada"
    assert retry_after > 0, "Retry-After debe ser mayor a 0"
    assert retry_after <= 2, "Retry-After no debe superar la ventana"


def test_aislamiento_entre_diferentes_ips(limiter):
    ip_a = "10.0.0.1"
    ip_b = "10.0.0.2"

    # Agotar límite de IP A
    for _ in range(5):
        limiter.verificar_limite(ip_a)

    permitido_a, _ = limiter.verificar_limite(ip_a)
    assert not permitido_a, "IP A debería estar bloqueada"

    # IP B no debe verse afectada por el tráfico de IP A
    permitido_b, _ = limiter.verificar_limite(ip_b)
    assert permitido_b, "IP B debe seguir teniendo acceso"

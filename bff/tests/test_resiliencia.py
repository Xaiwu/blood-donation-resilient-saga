import os
import sys
import threading
from unittest.mock import MagicMock, patch

import pytest
import requests

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from resiliencia import (  # noqa: E402
    Bulkhead,
    BulkheadLleno,
    CircuitBreaker,
    CircuitoAbierto,
    ClienteResiliente,
    Estado,
)


class Reloj:
    """Reloj controlado para avanzar el tiempo sin esperar."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def falla():
    raise RuntimeError("caida")


def fallar(cb, veces):
    for _ in range(veces):
        with pytest.raises(RuntimeError):
            cb.llamar(falla)


def respuesta(status_code):
    r = MagicMock()
    r.status_code = status_code
    return r


@pytest.fixture
def reloj():
    return Reloj()


# ---------------------------------------------------------------- Circuit Breaker


def test_tres_fallos_consecutivos_abren_el_circuito(reloj):
    cb = CircuitBreaker("x", umbral_fallos=3, reloj=reloj)
    fallar(cb, 2)
    assert cb.estado is Estado.CERRADO
    fallar(cb, 1)
    assert cb.estado is Estado.ABIERTO


def test_un_exito_reinicia_el_contador(reloj):
    cb = CircuitBreaker("x", umbral_fallos=3, reloj=reloj)
    fallar(cb, 2)
    assert cb.llamar(lambda: "ok") == "ok"
    fallar(cb, 2)
    assert cb.estado is Estado.CERRADO


def test_abierto_rechaza_sin_ejecutar(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, reloj=reloj)
    fallar(cb, 1)
    operacion = MagicMock()
    with pytest.raises(CircuitoAbierto):
        cb.llamar(operacion)
    operacion.assert_not_called()


def test_abierto_pasa_a_semiabierto_tras_la_ventana(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, tiempo_recuperacion_s=10, reloj=reloj)
    fallar(cb, 1)
    reloj.t += 9.9
    assert cb.estado is Estado.ABIERTO
    reloj.t += 0.1
    assert cb.estado is Estado.SEMIABIERTO


def test_semiabierto_cierra_si_la_prueba_funciona(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, tiempo_recuperacion_s=10, reloj=reloj)
    fallar(cb, 1)
    reloj.t += 10
    assert cb.llamar(lambda: "ok") == "ok"
    assert cb.estado is Estado.CERRADO


def test_semiabierto_vuelve_a_abrir_si_la_prueba_falla(reloj):
    cb = CircuitBreaker("x", umbral_fallos=3, tiempo_recuperacion_s=10, reloj=reloj)
    fallar(cb, 3)
    reloj.t += 10
    fallar(cb, 1)
    assert cb.estado is Estado.ABIERTO
    reloj.t += 9
    assert cb.estado is Estado.ABIERTO  # la ventana se reinició


def test_semiabierto_admite_una_sola_prueba(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, tiempo_recuperacion_s=10, reloj=reloj)
    fallar(cb, 1)
    reloj.t += 10

    en_curso, seguir = threading.Event(), threading.Event()

    def prueba_lenta():
        en_curso.set()
        seguir.wait(2)
        return "ok"

    hilo = threading.Thread(target=cb.llamar, args=(prueba_lenta,))
    hilo.start()
    en_curso.wait(2)
    with pytest.raises(CircuitoAbierto):
        cb.llamar(lambda: "otra")
    seguir.set()
    hilo.join()
    assert cb.estado is Estado.CERRADO


def test_resultado_tardio_no_cambia_el_estado_en_semiabierto(reloj):
    """Una llamada que empezó con el circuito CERRADO y termina en SEMIABIERTO no decide."""
    cb = CircuitBreaker("x", umbral_fallos=3, tiempo_recuperacion_s=10, reloj=reloj)
    en_curso, seguir = threading.Event(), threading.Event()

    def lenta():
        en_curso.set()
        seguir.wait(2)
        return "ok"

    hilo = threading.Thread(target=cb.llamar, args=(lenta,))
    hilo.start()
    en_curso.wait(2)
    fallar(cb, 3)
    reloj.t += 10
    assert cb.estado is Estado.SEMIABIERTO
    seguir.set()
    hilo.join()
    assert cb.estado is Estado.SEMIABIERTO


def test_excepcion_que_no_es_fallo_no_abre(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, reloj=reloj)

    def error_del_llamador():
        raise ValueError("400")

    with pytest.raises(ValueError):
        cb.llamar(error_del_llamador, es_fallo=lambda exc: not isinstance(exc, ValueError))
    assert cb.estado is Estado.CERRADO


def test_desactivado_deja_pasar_todo(reloj):
    cb = CircuitBreaker("x", umbral_fallos=1, activo=False, reloj=reloj)
    fallar(cb, 5)
    assert cb.estado is Estado.CERRADO


# ---------------------------------------------------------------- Bulkhead


def test_bulkhead_rechaza_la_sexta_llamada_simultanea():
    bh = Bulkhead("x", max_concurrentes=5)
    listas, seguir = threading.Barrier(6), threading.Event()

    def lenta():
        listas.wait(2)
        seguir.wait(2)

    hilos = [threading.Thread(target=bh.ejecutar, args=(lenta,)) for _ in range(5)]
    for h in hilos:
        h.start()
    listas.wait(2)  # las 5 están dentro del bulkhead
    with pytest.raises(BulkheadLleno):
        bh.ejecutar(lambda: "sexta")
    seguir.set()
    for h in hilos:
        h.join()
    assert bh.metricas["rechazadas"] == 1
    assert bh.metricas["max_en_curso_observado"] == 5


def test_bulkhead_libera_el_cupo_aunque_la_operacion_falle():
    bh = Bulkhead("x", max_concurrentes=1)
    with pytest.raises(RuntimeError):
        bh.ejecutar(falla)
    assert bh.ejecutar(lambda: "ok") == "ok"
    assert bh.en_curso == 0


def test_bulkhead_desactivado_no_rechaza():
    bh = Bulkhead("x", max_concurrentes=1, activo=False)
    listas, seguir = threading.Barrier(4), threading.Event()

    def lenta():
        listas.wait(2)
        seguir.wait(2)

    hilos = [threading.Thread(target=bh.ejecutar, args=(lenta,)) for _ in range(3)]
    for h in hilos:
        h.start()
    listas.wait(2)
    seguir.set()
    for h in hilos:
        h.join()
    assert bh.metricas["rechazadas"] == 0
    assert bh.metricas["max_en_curso_observado"] == 3


# ---------------------------------------------------------------- Cliente


def crear_cliente(reloj, umbral=3, max_concurrentes=5):
    return ClienteResiliente(
        CircuitBreaker("elegibilidad", umbral_fallos=umbral, tiempo_recuperacion_s=10, reloj=reloj),
        Bulkhead("elegibilidad", max_concurrentes=max_concurrentes),
    )


def test_cliente_exige_timeout(reloj):
    with pytest.raises(ValueError):
        crear_cliente(reloj).post("http://elegibilidad/validar", json={})


@patch("requests.post")
def test_cliente_tres_5xx_abren_y_luego_fail_fast(mock_post, reloj):
    mock_post.return_value = respuesta(503)
    cliente = crear_cliente(reloj)
    for _ in range(3):
        assert cliente.post("http://e", json={}, timeout=1).status_code == 503
    with pytest.raises(CircuitoAbierto):
        cliente.post("http://e", json={}, timeout=1)
    assert mock_post.call_count == 3


@patch("requests.post")
def test_cliente_timeout_y_conexion_cuentan_como_fallo(mock_post, reloj):
    mock_post.side_effect = [requests.Timeout("lento"), requests.ConnectionError("caido")]
    cliente = crear_cliente(reloj, umbral=2)
    with pytest.raises(requests.Timeout):
        cliente.post("http://e", json={}, timeout=1)
    with pytest.raises(requests.ConnectionError):
        cliente.post("http://e", json={}, timeout=1)
    assert cliente.circuit_breaker.estado is Estado.ABIERTO


@patch("requests.post")
def test_cliente_422_no_abre_el_circuito(mock_post, reloj):
    mock_post.return_value = respuesta(422)
    cliente = crear_cliente(reloj, umbral=1)
    for _ in range(3):
        assert cliente.post("http://e", json={}, timeout=1).status_code == 422
    assert cliente.circuit_breaker.estado is Estado.CERRADO


@patch("requests.post")
def test_cliente_se_recupera_cuando_elegibilidad_vuelve(mock_post, reloj):
    mock_post.return_value = respuesta(500)
    cliente = crear_cliente(reloj, umbral=1)
    cliente.post("http://e", json={}, timeout=1)
    assert cliente.circuit_breaker.estado is Estado.ABIERTO
    mock_post.return_value = respuesta(200)
    reloj.t += 10
    assert cliente.post("http://e", json={}, timeout=1).status_code == 200
    assert cliente.circuit_breaker.estado is Estado.CERRADO


def test_cliente_bulkhead_lleno_no_abre_el_circuito(reloj):
    cliente = crear_cliente(reloj, umbral=1, max_concurrentes=1)
    en_curso, seguir = threading.Event(), threading.Event()

    def post_lento(*_args, **_kwargs):
        en_curso.set()
        seguir.wait(2)
        return respuesta(200)

    with patch("requests.post", side_effect=post_lento):
        hilo = threading.Thread(target=cliente.post, args=("http://e",), kwargs={"timeout": 1})
        hilo.start()
        en_curso.wait(2)
        with pytest.raises(BulkheadLleno):
            cliente.post("http://e", json={}, timeout=1)
        seguir.set()
        hilo.join()
    assert cliente.circuit_breaker.estado is Estado.CERRADO


def test_desde_entorno(monkeypatch):
    monkeypatch.setenv("CB_UMBRAL_FALLOS", "4")
    monkeypatch.setenv("CB_ACTIVO", "false")
    monkeypatch.setenv("BULKHEAD_MAX_CONCURRENTES", "7")
    cliente = ClienteResiliente.desde_entorno()
    assert cliente.circuit_breaker.umbral_fallos == 4
    assert cliente.circuit_breaker.activo is False
    assert cliente.bulkhead.max_concurrentes == 7

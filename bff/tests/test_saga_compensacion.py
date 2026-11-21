import os
import sys
from unittest.mock import MagicMock, patch
import pytest
import requests

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
)
import saga
from resiliencia import ClienteResiliente
from saga import (
    CUPOS_SERVICE_URL,
    ELEGIBILIDAD_SERVICE_URL,
    REGISTRO_SERVICE_URL,
    HTTPException,
    ejecutar_saga_agendamiento,
)


@pytest.fixture(autouse=True)
def resetear_resiliencia():
    """Garantiza un Circuit Breaker y Bulkhead limpios antes de cada prueba."""
    saga.elegibilidad_resiliente = ClienteResiliente.desde_entorno()


@pytest.fixture
def payload_donacion():
    return {
        "rut_donante": "12345678-9",
        "nombre_donante": "Donante Test",
        "id_jornada": "JORNADA-1",
        "horario": "10:00",
        "edad": 28,
        "peso_kg": 72.5,
    }


@pytest.fixture
def id_saga_test():
    return "test-saga-12345"


@patch("requests.post")
def test_camino_feliz_saga_completa(mock_post, payload_donacion, id_saga_test):
    """Escenario 1: Todos los pasos responden OK -> Cita confirmada."""
    # Paso 1: Cupos OK
    resp_cupos = MagicMock()
    resp_cupos.status_code = 200
    resp_cupos.content = b'{"ok": true, "cupo_id": "c-1"}'
    resp_cupos.json.return_value = {"ok": True, "cupo_id": "c-1"}

    # Paso 2: Elegibilidad OK
    resp_eleg = MagicMock()
    resp_eleg.status_code = 200
    resp_eleg.content = b'{"apto": true}'
    resp_eleg.json.return_value = {"apto": True}

    # Paso 3: Registro Creado
    resp_reg = MagicMock()
    resp_reg.status_code = 201
    resp_reg.content = b'{"cita_id": "cita-99"}'
    resp_reg.json.return_value = {"cita_id": "cita-99"}

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_reg]

    resultado = ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert resultado["estado"] == "CONFIRMADA"
    assert resultado["id_saga"] == id_saga_test
    assert not resultado["compensado"]
    assert mock_post.call_count == 3


@patch("requests.post")
def test_fallo_paso_3_dispara_compensacion_inversa(
    mock_post, payload_donacion, id_saga_test
):
    """
    Escenario 2 de la Rúbrica:
    El Paso 3 (Registro) falla con HTTP 500.
    La Saga debe capturar la falla y compensar llamando a Cupos.liberar.
    """
    # Paso 1: Cupos OK
    resp_cupos = MagicMock()
    resp_cupos.status_code = 200
    resp_cupos.content = b'{"ok": true}'
    resp_cupos.json.return_value = {"ok": True}

    # Paso 2: Elegibilidad OK
    resp_eleg = MagicMock()
    resp_eleg.status_code = 200
    resp_eleg.content = b'{"apto": true}'
    resp_eleg.json.return_value = {"apto": True}

    # Paso 3: Registro falla (HTTP 500)
    resp_reg = MagicMock()
    resp_reg.status_code = 500
    resp_reg.content = b'{"error": "Fallo de base de datos"}'
    resp_reg.text = "Fallo de base de datos"

    # Compensación Paso 1: Liberar Cupo OK
    resp_liberar = MagicMock()
    resp_liberar.status_code = 200
    resp_liberar.content = b'{"liberado": true}'

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_reg, resp_liberar]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 500
    assert "compensada" in exc_info.value.detail.lower()

    # Verificar que la 4ta llamada a requests.post fue a la URL de compensación
    assert mock_post.call_count == 4
    url_llamada_compensacion = mock_post.call_args_list[3][0][0]
    assert url_llamada_compensacion == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("requests.post")
def test_donante_no_apto_dispara_compensacion(
    mock_post, payload_donacion, id_saga_test
):
    """
    Si el validador de elegibilidad retorna 422 (No apto),
    se debe liberar el cupo reservado y no registrar la donación.
    """
    # Paso 1: Cupos OK
    resp_cupos = MagicMock()
    resp_cupos.status_code = 200
    resp_cupos.content = b'{"ok": true}'

    # Paso 2: Elegibilidad 422
    resp_eleg = MagicMock()
    resp_eleg.status_code = 422
    resp_eleg.content = b'{"motivo": "Hemoglobina insuficiente"}'

    # Compensación: Liberar cupo
    resp_liberar = MagicMock()
    resp_liberar.status_code = 200

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_liberar]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 422
    # Verificar llamada a liberar cupo
    assert mock_post.call_count == 3
    assert mock_post.call_args_list[2][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("requests.post")
def test_degradacion_por_caida_elegibilidad(mock_post, payload_donacion, id_saga_test):
    """
    Regla de negocio 2.4:
    Si Elegibilidad cae (503/timeout), se degrada a PENDIENTE_VALIDACION sin colgarse.
    """
    # Paso 1: Cupos OK
    resp_cupos = MagicMock()
    resp_cupos.status_code = 200
    resp_cupos.content = b'{"ok": true}'

    # Paso 2: Elegibilidad 503 (caída temporal)
    resp_eleg = MagicMock()
    resp_eleg.status_code = 503
    resp_eleg.content = b'{"error": "Servicio externo inaccesible"}'

    # Paso 3: Registro OK
    resp_reg = MagicMock()
    resp_reg.status_code = 201
    resp_reg.content = b'{"cita_id": "c-123"}'
    resp_reg.json.return_value = {"cita_id": "c-123"}

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_reg]

    resultado = ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    # Debe completar con estado degradado, no abortar
    assert resultado["estado"] == "PENDIENTE_VALIDACION"
    assert resultado["elegibilidad_estado"] == "PENDIENTE_VALIDACION"
    assert mock_post.call_count == 3


@patch("saga.time.sleep")
@patch("requests.post")
def test_liberacion_fallida_reintenta_y_reporta_fallo_compensacion(
    mock_post, mock_sleep, payload_donacion, id_saga_test
):
    """
    Si POST /cupos/liberar falla con 500, reintenta 3 veces con backoff y NO miente diciendo 'cupo liberado'.
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_eleg = MagicMock(status_code=422, content=b'{"motivo": "No apto"}')
    resp_liberar_fail = MagicMock(status_code=500, text="DB caída")

    mock_post.side_effect = [
        resp_cupos,
        resp_eleg,
        resp_liberar_fail,
        resp_liberar_fail,
        resp_liberar_fail,
    ]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 422
    assert "falló la compensación automática en 'liberar_cupo'" in exc_info.value.detail
    assert "cupo liberado" not in exc_info.value.detail
    assert (
        mock_post.call_count == 5
    )  # 1 reservar + 1 elegibilidad + 3 reintentos liberar
    assert mock_sleep.call_count == 2  # backoff entre intento 1->2 y 2->3


@patch("requests.post")
def test_timeout_en_registro_anula_registro_y_luego_libera_cupo_en_orden_inverso(
    mock_post, payload_donacion, id_saga_test
):
    """
    Si Paso 3 (Registro) lanza Timeout (resultado incierto), la Saga primero anula el registro
    y luego libera el cupo en orden estrictamente inverso (LIFO).
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_eleg = MagicMock(status_code=200, content=b'{"apto": true}')
    resp_eleg.json.return_value = {"apto": True}
    resp_anular = MagicMock(status_code=200, content=b'{"anulado": true}')
    resp_liberar = MagicMock(status_code=200, content=b'{"liberado": true}')

    mock_post.side_effect = [
        resp_cupos,
        resp_eleg,
        requests.Timeout("Timeout en registro"),
        resp_anular,
        resp_liberar,
    ]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 500
    assert "compensada y cupo liberado" in exc_info.value.detail
    assert mock_post.call_count == 5
    # Orden inverso (LIFO): primero anular registro (paso 3), luego liberar cupo (paso 1)
    assert (
        mock_post.call_args_list[3][0][0]
        == f"{REGISTRO_SERVICE_URL}/registro/donaciones/{id_saga_test}/anular"
    )
    assert mock_post.call_args_list[4][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("requests.post")
def test_elegibilidad_400_compensa_y_200_apto_false_compensa(
    mock_post, payload_donacion, id_saga_test
):
    """
    Un 400 de Elegibilidad o un 200 con {"apto": false} NO se toma como APTO,
    sino que dispara compensación del cupo.
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_eleg_false = MagicMock(status_code=200, content=b'{"apto": false}')
    resp_eleg_false.json.return_value = {"apto": False}
    resp_liberar = MagicMock(status_code=200)

    mock_post.side_effect = [resp_cupos, resp_eleg_false, resp_liberar]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 422
    assert mock_post.call_args_list[2][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("requests.post")
def test_reintento_idempotente_200_en_registro_no_libera_cupo(
    mock_post, payload_donacion, id_saga_test
):
    """
    Si un reintento con la misma Idempotency-Key recibe 200 idempotente de Registro,
    la Saga lo trata como éxito y NO libera el cupo de la donación confirmada.
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true, "idempotente": true}')
    resp_cupos.json.return_value = {"ok": True, "idempotente": True}
    resp_eleg = MagicMock(status_code=200, content=b'{"apto": true}')
    resp_eleg.json.return_value = {"apto": True}
    resp_reg_dup = MagicMock(
        status_code=200, content=b'{"cita_id": "cita-99", "idempotente": true}'
    )
    resp_reg_dup.json.return_value = {"cita_id": "cita-99", "idempotente": True}

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_reg_dup]

    resultado = ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert resultado["estado"] == "CONFIRMADA"
    assert mock_post.call_count == 3  # No se llamó a /cupos/liberar


@patch("requests.post")
def test_colision_409_en_registro_compensa_cupo(
    mock_post, payload_donacion, id_saga_test
):
    """
    Issue #12: Si Registro responde 409 (colisión de idempotencia con datos distintos),
    la Saga debe abortar con 409 y compensar liberando el cupo.
    """
    resp_cupos = MagicMock(status_code=201, content=b'{"ok": true, "idempotente": false}')
    resp_cupos.json.return_value = {"ok": True, "idempotente": False}
    resp_eleg = MagicMock(status_code=200, content=b'{"apto": true}')
    resp_eleg.json.return_value = {"apto": True}
    resp_reg_409 = MagicMock(status_code=409, text="Colisión de id_saga")
    resp_liberar = MagicMock(status_code=200, content=b'{"liberado": true}')

    mock_post.side_effect = [resp_cupos, resp_eleg, resp_reg_409, resp_liberar]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 409
    assert "compensada y cupo liberado" in exc_info.value.detail
    assert mock_post.call_count == 4
    assert mock_post.call_args_list[3][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("saga.time.sleep")
@patch("requests.post")
def test_fallo_anulacion_registro_detiene_compensacion_sin_liberar_cupo(
    mock_post, mock_sleep, payload_donacion, id_saga_test
):
    """
    Si Paso 3 (Registro) da timeout y la compensación 'anular_registro' falla los 3 intentos,
    _compensar debe detener la cadena LIFO y NO liberar el cupo para evitar donación sin cupo.
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_eleg = MagicMock(status_code=200, content=b'{"apto": true}')
    resp_eleg.json.return_value = {"apto": True}
    resp_anular_fail = MagicMock(status_code=500, text="Registro caído")

    mock_post.side_effect = [
        resp_cupos,
        resp_eleg,
        requests.Timeout("Timeout en registro"),
        resp_anular_fail,
        resp_anular_fail,
        resp_anular_fail,
    ]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 500
    assert "anular_registro" in exc_info.value.detail
    assert "se detuvo la liberación del cupo" in exc_info.value.detail
    assert "cupo liberado" not in exc_info.value.detail
    # 1 reservar + 1 elegibilidad + 1 timeout registro + 3 reintentos anular (0 liberar cupo)
    assert mock_post.call_count == 6
    urls_llamadas = [call[0][0] for call in mock_post.call_args_list]
    assert f"{CUPOS_SERVICE_URL}/cupos/liberar" not in urls_llamadas
    assert mock_sleep.call_count == 2


@patch("requests.post")
def test_circuit_breaker_abre_y_degrada_en_saga(mock_post, payload_donacion):
    """
    Escenario 3 de la Rúbrica (integración Saga + Circuit Breaker):
    Tras 3 fallos consecutivos (503) de Elegibilidad, el circuito se abre y la 4ta
    llamada degrada a PENDIENTE_VALIDACION sin invocar a Elegibilidad (fail-fast).
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_eleg_503 = MagicMock(status_code=503, content=b'{"error": "caido"}')
    resp_reg = MagicMock(status_code=201, content=b'{"cita_id": "c-1"}')
    resp_reg.json.return_value = {"cita_id": "c-1"}

    # 3 primeras ejecuciones: Cupos(200), Elegibilidad(503), Registro(201) -> 9 llamadas
    # 4ta ejecución: Cupos(200), Elegibilidad(RECHAZADA por CB sin llamar), Registro(201) -> 2 llamadas
    mock_post.side_effect = [
        resp_cupos, resp_eleg_503, resp_reg,
        resp_cupos, resp_eleg_503, resp_reg,
        resp_cupos, resp_eleg_503, resp_reg,
        resp_cupos, resp_reg,
    ]

    for i in range(4):
        res = ejecutar_saga_agendamiento(payload_donacion, f"saga-cb-{i}")
        assert res["estado"] == "PENDIENTE_VALIDACION"
        assert res["elegibilidad_estado"] == "PENDIENTE_VALIDACION"

    assert mock_post.call_count == 11
    llamadas_eleg = [
        call[0][0]
        for call in mock_post.call_args_list
        if call[0][0] == f"{ELEGIBILIDAD_SERVICE_URL}/elegibilidad/validar"
    ]
    assert len(llamadas_eleg) == 3  # la 4ta no tocó Elegibilidad
    assert saga.elegibilidad_resiliente.circuit_breaker.metricas["rechazos_fail_fast"] == 1


@patch("requests.post")
def test_reintento_tras_saga_degradada_no_apto_anula_registro_y_libera_cupo(
    mock_post, payload_donacion, id_saga_test
):
    """
    Issue #11: Si una Saga previa degradó a PENDIENTE_VALIDACION (cupo reservado y cita creada),
    y un reintento con la misma Idempotency-Key encuentra a Elegibilidad recuperada respondiendo 422,
    Cupos responde idempotente=True y la compensación LIFO debe anular la cita previa Y liberar el cupo.
    """
    resp_cupos_idem = MagicMock(
        status_code=200, content=b'{"ok": true, "idempotente": true}'
    )
    resp_cupos_idem.json.return_value = {"ok": True, "idempotente": True}
    resp_eleg_422 = MagicMock(status_code=422, content=b'{"motivo": "No apto"}')
    resp_anular = MagicMock(status_code=200, content=b'{"anulado": true}')
    resp_liberar = MagicMock(status_code=200, content=b'{"liberado": true}')

    mock_post.side_effect = [resp_cupos_idem, resp_eleg_422, resp_anular, resp_liberar]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 422
    assert "compensada y cupo liberado" in exc_info.value.detail
    assert mock_post.call_count == 4
    assert (
        mock_post.call_args_list[2][0][0]
        == f"{REGISTRO_SERVICE_URL}/registro/donaciones/{id_saga_test}/anular"
    )
    assert mock_post.call_args_list[3][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"


@patch("saga.time.sleep")
@patch("requests.post")
def test_timeout_en_reservar_cupo_reintenta_404_y_reporta_fallo_si_no_libera(
    mock_post, mock_sleep, payload_donacion, id_saga_test
):
    """
    Issue #13: Ante Timeout en Paso 1 (Cupos), la liberación preventiva NO acepta 404 como éxito,
    reintenta con backoff y, si sigue sin confirmar liberación, reporta fallo de reconciliación.
    """
    resp_404 = MagicMock(status_code=404, text="No encontrada aún")
    mock_post.side_effect = [
        requests.Timeout("Timeout al reservar"),
        resp_404,
        resp_404,
        resp_404,
    ]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 503
    assert "falló la liberación preventiva automática" in exc_info.value.detail
    assert "requiere reconciliación" in exc_info.value.detail
    assert mock_post.call_count == 4  # 1 reservar timeout + 3 reintentos liberar
    assert mock_sleep.call_count == 2


@patch("saga.time.sleep")
@patch("requests.post")
def test_timeout_en_registro_con_404_cancelacion_persistida_libera_cupo(
    mock_post, mock_sleep, payload_donacion, id_saga_test
):
    """
    Si hay Timeout en registro, si Registro responde 404
    (porque la cita no existía y persistió la cancelación en sagas_canceladas),
    el BFF reconoce el 404 como compensación exitosa en el primer intento y
    continúa la cadena LIFO liberando el cupo.
    """
    resp_cupos = MagicMock(status_code=200, content=b'{"ok": true}')
    resp_cupos.json.return_value = {"ok": True}
    resp_eleg = MagicMock(status_code=200, content=b'{"apto": true}')
    resp_eleg.json.return_value = {"apto": True}
    resp_anular_404 = MagicMock(
        status_code=404,
        text='{"detail": "No se encontró ninguna cita registrada para el id_saga indicado."}',
    )
    resp_liberar = MagicMock(status_code=200, content=b'{"liberado": true}')

    mock_post.side_effect = [
        resp_cupos,
        resp_eleg,
        requests.Timeout("Timeout en registro"),
        resp_anular_404,
        resp_liberar,
    ]

    with pytest.raises(HTTPException) as exc_info:
        ejecutar_saga_agendamiento(payload_donacion, id_saga_test)

    assert exc_info.value.status_code == 500
    assert "Transacción compensada y cupo liberado." in exc_info.value.detail
    assert mock_post.call_count == 5
    assert (
        mock_post.call_args_list[3][0][0]
        == f"{REGISTRO_SERVICE_URL}/registro/donaciones/{id_saga_test}/anular"
    )
    assert mock_post.call_args_list[4][0][0] == f"{CUPOS_SERVICE_URL}/cupos/liberar"
    assert mock_sleep.call_count == 0

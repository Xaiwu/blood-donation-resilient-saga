import os
import sys
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))
from saga import (
    CUPOS_SERVICE_URL,
    HTTPException,
    ejecutar_saga_agendamiento,
)


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
def test_fallo_paso_3_dispara_compensacion_inversa(mock_post, payload_donacion, id_saga_test):
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
def test_donante_no_apto_dispara_compensacion(mock_post, payload_donacion, id_saga_test):
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
def test_degradacion_elegante_por_caida_elegibilidad(mock_post, payload_donacion, id_saga_test):
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

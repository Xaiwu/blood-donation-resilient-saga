import os
import sys
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient
import pytest
import requests

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
)
from src.main import app  # noqa: E402
from src.models import SolicitudAgendarDonacion  # noqa: E402


client = TestClient(app)


def test_modelo_permite_edad_y_peso_para_que_decida_elegibilidad():
    """Issue #17: El BFF no rechaza por reglas clínicas (17 años, 45 kg); decide Elegibilidad."""
    solicitud = SolicitudAgendarDonacion(
        rut_donante="12345678-9",
        nombre_donante="Joven Test",
        id_jornada="JORNADA-01",
        horario="09:00",
        edad=17,
        peso_kg=45.0,
    )
    assert solicitud.edad == 17
    assert solicitud.peso_kg == 45.0


@patch("src.main.requests.get")
def test_resumen_donante_completo(mock_get):
    """Issue #15: Ambos servicios responden 200 -> datos_parciales=False y ultima_cita."""
    resp_reg = MagicMock(
        status_code=200,
        content=b'[{"id_cita": "c-1", "nombre_donante": "Ana", "estado_elegibilidad": "APTO"}]',
    )
    resp_reg.json.return_value = [
        {"id_cita": "c-1", "nombre_donante": "Ana", "estado_elegibilidad": "APTO"}
    ]
    resp_cupos = MagicMock(
        status_code=200,
        content=b'[{"id_cupo": "cup-1", "id_jornada": "JORNADA-01"}]',
    )
    resp_cupos.json.return_value = [{"id_cupo": "cup-1", "id_jornada": "JORNADA-01"}]

    mock_get.side_effect = [resp_reg, resp_cupos]

    resp = client.get("/api/v1/donantes/12345678-9/resumen")
    assert resp.status_code == 200
    data = resp.json()
    assert data["datos_parciales"] is False
    assert data["advertencias"] == []
    assert data["ultima_cita"]["id_cita"] == "c-1"
    assert len(data["cupos_activos"]) == 1


@patch("src.main.requests.get")
def test_resumen_donante_fallo_parcial_incluye_advertencia(mock_get):
    """Issue #15: Si falla Cupos y responde Registro, retorna 200 con datos_parciales=True."""
    resp_reg = MagicMock(
        status_code=200,
        content=b'[{"id_cita": "c-1", "nombre_donante": "Ana"}]',
    )
    resp_reg.json.return_value = [{"id_cita": "c-1", "nombre_donante": "Ana"}]

    def _side_effect(url, **_kwargs):
        if "/registro/" in url:
            return resp_reg
        raise requests.Timeout("cupos lento")

    mock_get.side_effect = _side_effect

    resp = client.get("/api/v1/donantes/12345678-9/resumen")
    assert resp.status_code == 200
    data = resp.json()
    assert data["datos_parciales"] is True
    assert len(data["advertencias"]) == 1
    assert "cupos-service" in data["advertencias"][0]
    assert data["ultima_cita"]["id_cita"] == "c-1"


@patch("src.main.requests.get")
def test_resumen_donante_ambos_caidos_retorna_503(mock_get):
    """Issue #15: Si fallan Registro y Cupos a la vez, responde 503 Service Unavailable."""
    mock_get.side_effect = requests.ConnectionError("caidos")

    resp = client.get("/api/v1/donantes/12345678-9/resumen")
    assert resp.status_code == 503
    assert "registro-service" in resp.json()["detail"]
    assert "cupos-service" in resp.json()["detail"]

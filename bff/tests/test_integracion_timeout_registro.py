import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import requests
from fastapi.testclient import TestClient

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BFF_SRC = os.path.join(REPO_ROOT, "bff", "src")
REGISTRO_ROOT = os.path.join(REPO_ROOT, "registro-service")

if BFF_SRC not in sys.path:
    sys.path.insert(0, BFF_SRC)

import saga  # noqa: E402
from resiliencia import ClienteResiliente  # noqa: E402
from saga import (  # noqa: E402
    CUPOS_SERVICE_URL,
    ELEGIBILIDAD_SERVICE_URL,
    HTTPException,
    REGISTRO_SERVICE_URL,
    ejecutar_saga_agendamiento,
)


@pytest.fixture
def registro_client(tmp_path, monkeypatch):
    """Levanta la app real de registro-service respaldada por un SQLite temporal."""
    db_file = tmp_path / "registro_integracion.db"
    monkeypatch.setenv("DATABASE_PATH", str(db_file))
    monkeypatch.syspath_prepend(REGISTRO_ROOT)

    # Limpia posibles módulos cacheados bajo 'src' para cargar registro-service limpio
    prev_src_modules = {
        name: mod
        for name, mod in sys.modules.items()
        if name == "src" or name.startswith("src.")
    }
    for name in prev_src_modules:
        sys.modules.pop(name, None)

    try:
        from src.db import init_db  # noqa: E402
        from src.main import app as registro_app  # noqa: E402

        init_db(str(db_file))
        with TestClient(registro_app) as client:
            yield client
    finally:
        for name in list(sys.modules):
            if name == "src" or name.startswith("src."):
                sys.modules.pop(name, None)
        sys.modules.update(prev_src_modules)


def test_integracion_timeout_registro_404_persiste_cancelacion_y_libera_cupo(
    registro_client,
):
    """
    Prueba de integración BFF <-> registro-service ante timeout en Paso 3:
    1. Cupos reserva 1 cupo (disponibles: 1 -> 0).
    2. Elegibilidad valida al donante.
    3. Paso 3 (Registro) sufre un Timeout antes de crear la cita.
    4. BFF compensa llamando a /registro/donaciones/{id_saga}/anular en Registro real:
       Registro persiste la cancelación en sagas_canceladas y responde 404.
    5. BFF reconoce el 404 como compensación exitosa y libera el cupo (disponibles: 0 -> 1).
    6. Un reintento tardío de registro con el mismo id_saga es rechazado con 409.
    """
    saga.elegibilidad_resiliente = ClienteResiliente.desde_entorno()

    estado_cupos = {"disponibles": 1}
    id_saga = "saga-integ-timeout-404"
    payload = {
        "rut_donante": "12345678-9",
        "nombre_donante": "Donante Integracion",
        "id_jornada": "JORNADA-1",
        "horario": "10:00",
        "edad": 30,
        "peso_kg": 70.0,
    }

    def fake_requests_post(url, json=None, headers=None, timeout=None):
        if url == f"{CUPOS_SERVICE_URL}/cupos/reservar":
            estado_cupos["disponibles"] -= 1
            resp = MagicMock(status_code=200, content=b'{"ok": true}')
            resp.json.return_value = {"ok": True}
            return resp

        if url == f"{CUPOS_SERVICE_URL}/cupos/liberar":
            estado_cupos["disponibles"] += 1
            resp = MagicMock(status_code=200, content=b'{"liberado": true}')
            resp.json.return_value = {"liberado": True}
            return resp

        if url == f"{ELEGIBILIDAD_SERVICE_URL}/elegibilidad/validar":
            resp = MagicMock(status_code=200, content=b'{"apto": true}')
            resp.json.return_value = {"apto": True}
            return resp

        if url == f"{REGISTRO_SERVICE_URL}/registro/donaciones":
            raise requests.Timeout("Timeout simulado en registro-service")

        if url == f"{REGISTRO_SERVICE_URL}/registro/donaciones/{id_saga}/anular":
            r = registro_client.post(f"/registro/donaciones/{id_saga}/anular")
            resp = MagicMock(status_code=r.status_code, content=r.content, text=r.text)
            resp.json.return_value = r.json()
            return resp

        raise AssertionError(f"URL inesperada en prueba de integración: {url}")

    with patch("requests.post", side_effect=fake_requests_post):
        with pytest.raises(HTTPException) as exc_info:
            ejecutar_saga_agendamiento(payload, id_saga)

    assert exc_info.value.status_code == 500
    assert "Transacción compensada y cupo liberado." in exc_info.value.detail
    assert estado_cupos["disponibles"] == 1

    # Verificar que Registro rechazará cualquier llegada tardía del mismo id_saga (409)
    respuesta_tardia = registro_client.post(
        "/registro/donaciones",
        json={**payload, "id_saga": id_saga, "estado_elegibilidad": "APTO"},
    )
    assert respuesta_tardia.status_code == 409

    # Verificar que no quedó ninguna cita activa para el donante
    citas = registro_client.get(f"/registro/donantes/{payload['rut_donante']}")
    assert citas.status_code == 200
    assert citas.json() == []

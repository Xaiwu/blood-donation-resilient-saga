"""Pruebas unitarias de persistencia, idempotencia, compensación y endpoints HTTP para registro-service."""

import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (
    CitaNoEncontradaError,
    ColisionIdempotenciaError,
    anular_donacion,
    init_db,
    obtener_citas_donante,
    registrar_donacion,
)

from unittest.mock import patch

try:
    from fastapi.testclient import TestClient
    from src.main import app

    FASTAPI_DISPONIBLE = True
except ImportError:
    FASTAPI_DISPONIBLE = False


class TestRegistroPersistence(unittest.TestCase):
    """Pruebas sobre src/db.py (SQLite WAL + BEGIN IMMEDIATE)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_registro.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_registro_nuevo_e_idempotente(self) -> None:
        """Verifica que el primer registro crea la cita y el reintento idempotente retorna la misma sin duplicar."""
        r1 = registrar_donacion("saga-01", "12.345.678-9", "Ana Pérez", "JORNADA-01", "09:00", "APTO", self.db_path)
        self.assertTrue(r1["ok"])
        self.assertFalse(r1["idempotente"])
        self.assertEqual(r1["estado"], "CONFIRMADA")

        r2 = registrar_donacion("saga-01", "12.345.678-9", "Ana Pérez", "JORNADA-01", "09:00", "APTO", self.db_path)
        self.assertTrue(r2["idempotente"])
        self.assertEqual(r1["cita_id"], r2["cita_id"])
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 1)

    def test_colision_idempotencia_409(self) -> None:
        """Verifica que reutilizar un id_saga con otro donante o jornada lanza ColisionIdempotenciaError (409)."""
        registrar_donacion("saga-col", "12.345.678-9", "Ana", "JORNADA-01", "09:00", "APTO", self.db_path)
        with self.assertRaises(ColisionIdempotenciaError):
            registrar_donacion("saga-col", "99.999.999-9", "Ana", "JORNADA-01", "09:00", "APTO", self.db_path)

        with self.assertRaises(ColisionIdempotenciaError):
            registrar_donacion("saga-col", "12.345.678-9", "Ana", "JORNADA-02", "09:00", "APTO", self.db_path)

    def test_degradacion_pendiente_validacion_y_check_integrity_error(self) -> None:
        """Verifica PENDIENTE_VALIDACION y que una violación CHECK de SQLite no se disfraza como 409."""
        r_deg = registrar_donacion(
            "saga-deg", "12.345.678-9", "Ana", "JORNADA-01", "10:30", "PENDIENTE_VALIDACION", self.db_path
        )
        self.assertEqual(r_deg["estado"], "PENDIENTE_VALIDACION")

        with self.assertRaises(sqlite3.IntegrityError):
            registrar_donacion("saga-inv", "12.345.678-9", "Ana", "JORNADA-01", "11:00", "NO_APTO", self.db_path)

    def test_anulacion_idempotente_y_no_encontrada_404(self) -> None:
        """Verifica que anular cambia el estado una única vez y lanza 404 si la SAGA no existe."""
        registrar_donacion("saga-anular", "12.345.678-9", "Ana", "JORNADA-01", "09:00", "APTO", self.db_path)
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 1)

        a1 = anular_donacion("saga-anular", db_path=self.db_path)
        self.assertFalse(a1["idempotente"])
        self.assertEqual(a1["estado"], "ANULADA")
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 0)

        a2 = anular_donacion("saga-anular", db_path=self.db_path)
        self.assertTrue(a2["idempotente"])
        self.assertEqual(a2["estado"], "ANULADA")

        with self.assertRaises(CitaNoEncontradaError):
            anular_donacion("saga-inexistente", db_path=self.db_path)

    def test_ciclo_registrar_anular_reregistrar_anular(self) -> None:
        """Verifica que tras compensar y re-registrar el mismo id_saga, una nueva anulación vuelve a anular la cita."""
        r1 = registrar_donacion("saga-ciclo", "12.345.678-9", "Ana Pérez", "JORNADA-01", "11:00", "APTO", self.db_path)
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 1)

        a1 = anular_donacion("saga-ciclo", db_path=self.db_path)
        self.assertFalse(a1["idempotente"])
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 0)

        r2 = registrar_donacion("saga-ciclo", "12.345.678-9", "Ana Pérez", "JORNADA-01", "11:00", "APTO", self.db_path)
        self.assertFalse(r2["idempotente"])
        self.assertEqual(r1["cita_id"], r2["cita_id"])
        self.assertEqual(r2["estado"], "CONFIRMADA")
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 1)

        a2 = anular_donacion("saga-ciclo", db_path=self.db_path)
        self.assertFalse(a2["idempotente"])
        self.assertEqual(a2["estado"], "ANULADA")
        self.assertEqual(len(obtener_citas_donante("12.345.678-9", db_path=self.db_path)), 0)


@unittest.skipUnless(FASTAPI_DISPONIBLE, "FastAPI/TestClient no instalado en el entorno local")
class TestRegistroEndpoints(unittest.TestCase):
    """Pruebas de contrato HTTP con TestClient (X-Saga-ID, validación de campos obligatorios y compensación)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_endpoints.db")
        os.environ["DATABASE_PATH"] = self.db_path
        init_db(self.db_path)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        os.environ.pop("DATABASE_PATH", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_contrato_saga_con_header_x_saga_id_y_campos_obligatorios(self) -> None:
        """Verifica registro enviando X-Saga-ID por cabecera, rechazo 422 si faltan campos y anulación con body."""
        # 1. Si faltan nombre_donante, id_jornada u horario -> 422
        resp_incompleto = self.client.post(
            "/registro/donaciones",
            json={"rut_donante": "12.345.678-9"},
            headers={"X-Saga-ID": "saga-hdr-01"},
        )
        self.assertEqual(resp_incompleto.status_code, 422)

        # 2. Registro enviando id_saga únicamente en la cabecera X-Saga-ID -> 201
        payload_saga = {
            "rut_donante": "12.345.678-9",
            "nombre_donante": "Ana Pérez",
            "id_jornada": "JORNADA-01",
            "horario": "09:00",
            "estado_elegibilidad": "APTO",
        }
        resp_creado = self.client.post(
            "/registro/donaciones",
            json=payload_saga,
            headers={"X-Saga-ID": "saga-hdr-01"},
        )
        self.assertEqual(resp_creado.status_code, 201)
        self.assertEqual(resp_creado.json()["id_saga"], "saga-hdr-01")
        self.assertFalse(resp_creado.json()["idempotente"])

        # 3. Reintento idempotente con X-Saga-ID -> 200
        resp_idem = self.client.post(
            "/registro/donaciones",
            json=payload_saga,
            headers={"X-Saga-ID": "saga-hdr-01"},
        )
        self.assertEqual(resp_idem.status_code, 200)
        self.assertTrue(resp_idem.json()["idempotente"])

        # 4. Consulta por donante -> 200 con 1 cita activa
        resp_get = self.client.get("/registro/donantes/12.345.678-9")
        self.assertEqual(resp_get.status_code, 200)
        self.assertEqual(len(resp_get.json()), 1)

        # 5. Anulación enviando body como lo hace la Saga -> 200
        resp_anular = self.client.post(
            "/registro/donaciones/saga-hdr-01/anular",
            json=payload_saga,
            headers={"X-Saga-ID": "saga-hdr-01"},
        )
        self.assertEqual(resp_anular.status_code, 200)
        self.assertEqual(resp_anular.json()["estado"], "ANULADA")

        # 6. Si ocurre un IntegrityError distinto de UNIQUE (ej. CHECK), responde 500 y no 409
        with patch("src.main.registrar_donacion", side_effect=sqlite3.IntegrityError("CHECK constraint failed")):
            resp_check = self.client.post(
                "/registro/donaciones",
                json=payload_saga,
                headers={"X-Saga-ID": "saga-hdr-02"},
            )
            self.assertEqual(resp_check.status_code, 500)

    def test_falla_simulada_no_persiste_ni_bloquea_compensacion(self) -> None:
        """Con la falla activa, registrar responde 5xx sin guardar; anular, consultar y /health siguen funcionando."""
        payload = {
            "rut_donante": "12.345.678-9",
            "nombre_donante": "Ana Pérez",
            "id_jornada": "JORNADA-01",
            "horario": "09:00",
        }
        self.client.post("/registro/donaciones", json=payload, headers={"X-Saga-ID": "saga-previa"})

        r_cfg = self.client.post("/config/falla", json={"simular_falla": True, "codigo_falla": 503})
        self.assertEqual(r_cfg.status_code, 200)
        self.assertTrue(self.client.get("/config").json()["simular_falla"])
        self.assertEqual(self.client.post("/config/falla", json={}).status_code, 422)

        try:
            r_caido = self.client.post("/registro/donaciones", json=payload, headers={"X-Saga-ID": "saga-caida"})
            self.assertEqual(r_caido.status_code, 503)
            self.assertEqual(self.client.post("/registro/donaciones/saga-caida/anular").status_code, 404)
            self.assertEqual(len(self.client.get("/registro/donantes/12.345.678-9").json()), 1)
            self.assertEqual(self.client.get("/health").status_code, 200)
            self.assertEqual(self.client.post("/registro/donaciones/saga-previa/anular").status_code, 200)
        finally:
            r_reset = self.client.post("/config/reset")
        self.assertFalse(r_reset.json()["simular_falla"])

        r_ok = self.client.post("/registro/donaciones", json=payload, headers={"X-Saga-ID": "saga-caida"})
        self.assertEqual(r_ok.status_code, 201)

    @patch("src.caos.time.sleep")
    def test_respuesta_tardia_graba_antes_de_demorar(self, mock_sleep) -> None:
        """Con respuesta tardía, la cita se graba antes de la demora y la anulación posterior la revierte."""
        payload = {
            "rut_donante": "12.345.678-9",
            "nombre_donante": "Ana Pérez",
            "id_jornada": "JORNADA-01",
            "horario": "09:00",
        }
        self.assertEqual(self.client.post("/config/latencia", json={}).status_code, 422)
        self.assertEqual(self.client.post("/config/latencia", json={"latencia_segundos": 61}).status_code, 422)
        r_cfg = self.client.post("/config/latencia", json={"latencia_segundos": 5})
        self.assertEqual(r_cfg.json()["latencia_respuesta_segundos"], 5.0)

        try:
            grabado_antes_de_demorar = []
            mock_sleep.side_effect = lambda _s: grabado_antes_de_demorar.append(
                len(obtener_citas_donante("12.345.678-9", db_path=self.db_path))
            )
            r = self.client.post("/registro/donaciones", json=payload, headers={"X-Saga-ID": "saga-lenta"})
            self.assertEqual(r.status_code, 201)
            mock_sleep.assert_called_once_with(5.0)
            self.assertEqual(grabado_antes_de_demorar, [1])

            self.assertEqual(self.client.post("/registro/donaciones/saga-lenta/anular").status_code, 200)
            self.assertEqual(len(self.client.get("/registro/donantes/12.345.678-9").json()), 0)
        finally:
            r_reset = self.client.post("/config/reset")
        self.assertEqual(r_reset.json()["latencia_respuesta_segundos"], 0.0)


if __name__ == "__main__":
    unittest.main()

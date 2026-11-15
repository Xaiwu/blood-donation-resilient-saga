"""Pruebas unitarias de persistencia, idempotencia y compensación para registro-service."""

import os
from pathlib import Path
import shutil
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
        """Verifica que reutilizar un id_saga con otro donante o elegibilidad lanza ColisionIdempotenciaError (409)."""
        registrar_donacion("saga-col", "12.345.678-9", "Ana", "JORNADA-01", "09:00", "APTO", self.db_path)
        with self.assertRaises(ColisionIdempotenciaError):
            registrar_donacion("saga-col", "99.999.999-9", "Ana", "JORNADA-01", "09:00", "APTO", self.db_path)

        with self.assertRaises(ColisionIdempotenciaError):
            registrar_donacion("saga-col", "12.345.678-9", "Ana", "JORNADA-01", "09:00", "PENDIENTE_VALIDACION", self.db_path)

    def test_degradacion_pendiente_validacion(self) -> None:
        """Verifica el estado PENDIENTE_VALIDACION cuando Elegibilidad degrada."""
        r_deg = registrar_donacion(
            "saga-deg", "12.345.678-9", "Ana", "JORNADA-01", "10:30", "PENDIENTE_VALIDACION", self.db_path
        )
        self.assertEqual(r_deg["estado"], "PENDIENTE_VALIDACION")

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


if __name__ == "__main__":
    unittest.main()

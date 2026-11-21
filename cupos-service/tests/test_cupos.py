"""Pruebas unitarias de persistencia, idempotencia y compensación para cupos-service."""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.db import (
    ColisionIdempotenciaError,
    JornadaNoEncontradaError,
    JornadaSinCuposError,
    ReservaNoEncontradaError,
    SagaCanceladaError,
    get_connection,
    init_db,
    liberar_cupo,
    obtener_cupos_donante,
    reservar_cupo,
)


class TestCuposPersistence(unittest.TestCase):
    """Pruebas sobre src/db.py (SQLite WAL + BEGIN IMMEDIATE)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_cupos.db")
        init_db(self.db_path)

    def tearDown(self) -> None:
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _stock_disponible(self, id_jornada: str) -> int:
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT cupos_disponibles FROM jornadas WHERE id_jornada = ?",
                (id_jornada,),
            ).fetchone()
            return int(row["cupos_disponibles"])
        finally:
            conn.close()

    def test_reserva_nueva_e_idempotente(self) -> None:
        """Verifica que la primera reserva descuenta stock y el reintento idempotente no vuelve a descontar."""
        r1 = reservar_cupo("12.345.678-9", "JORNADA-01", "09:00", id_saga="saga-01", db_path=self.db_path)
        self.assertTrue(r1["ok"])
        self.assertFalse(r1["idempotente"])
        self.assertEqual(self._stock_disponible("JORNADA-01"), 9)

        r2 = reservar_cupo("12.345.678-9", "JORNADA-01", "09:00", id_saga="saga-01", db_path=self.db_path)
        self.assertTrue(r2["idempotente"])
        self.assertEqual(r1["cupo_id"], r2["cupo_id"])
        self.assertEqual(self._stock_disponible("JORNADA-01"), 9)

    def test_colision_idempotencia_409(self) -> None:
        """Verifica que reutilizar un id_saga con otro donante o jornada lanza ColisionIdempotenciaError (409)."""
        reservar_cupo("12.345.678-9", "JORNADA-01", "09:00", id_saga="saga-col", db_path=self.db_path)
        with self.assertRaises(ColisionIdempotenciaError):
            reservar_cupo("99.999.999-9", "JORNADA-01", "09:00", id_saga="saga-col", db_path=self.db_path)

    def test_rechazo_sin_stock_409_y_jornada_inexistente_404(self) -> None:
        """Verifica el rechazo 409 cuando se agota el stock y 404 cuando la jornada no existe."""
        reservar_cupo("12.345.678-9", "JORNADA-UNICA", "10:00", id_saga="saga-u1", db_path=self.db_path)
        self.assertEqual(self._stock_disponible("JORNADA-UNICA"), 0)

        with self.assertRaises(JornadaSinCuposError):
            reservar_cupo("22.222.222-2", "JORNADA-UNICA", "10:00", id_saga="saga-u2", db_path=self.db_path)

        with self.assertRaises(JornadaNoEncontradaError):
            reservar_cupo("12.345.678-9", "JORNADA-NO-EXISTE", "10:00", id_saga="saga-404", db_path=self.db_path)

    def test_liberacion_idempotente_y_no_encontrada_404(self) -> None:
        """Verifica que liberar restituye stock una única vez y lanza 404 si la SAGA no existe."""
        reservar_cupo("12.345.678-9", "JORNADA-01", "09:00", id_saga="saga-lib", db_path=self.db_path)
        self.assertEqual(self._stock_disponible("JORNADA-01"), 9)

        l1 = liberar_cupo("saga-lib", db_path=self.db_path)
        self.assertFalse(l1["idempotente"])
        self.assertEqual(l1["estado"], "LIBERADO")
        self.assertEqual(self._stock_disponible("JORNADA-01"), 10)

        l2 = liberar_cupo("saga-lib", db_path=self.db_path)
        self.assertTrue(l2["idempotente"])
        self.assertEqual(self._stock_disponible("JORNADA-01"), 10)

        with self.assertRaises(ReservaNoEncontradaError):
            liberar_cupo("saga-inexistente", db_path=self.db_path)

    def test_ciclo_reservar_liberar_rereservar_liberar(self) -> None:
        """Verifica que tras compensar y re-reservar el mismo id_saga, una nueva liberación sí devuelve el stock."""
        # 1. Reservar deja stock en 9
        r1 = reservar_cupo("12.345.678-9", "JORNADA-01", "11:00", id_saga="saga-ciclo", db_path=self.db_path)
        self.assertEqual(self._stock_disponible("JORNADA-01"), 9)

        # 2. Liberar devuelve stock a 10
        l1 = liberar_cupo("saga-ciclo", db_path=self.db_path)
        self.assertFalse(l1["idempotente"])
        self.assertEqual(self._stock_disponible("JORNADA-01"), 10)

        # 3. Re-reservar el mismo id_saga reutiliza el cupo y baja stock a 9
        r2 = reservar_cupo("12.345.678-9", "JORNADA-01", "11:00", id_saga="saga-ciclo", db_path=self.db_path)
        self.assertFalse(r2["idempotente"])
        self.assertEqual(r1["cupo_id"], r2["cupo_id"])
        self.assertEqual(self._stock_disponible("JORNADA-01"), 9)
        self.assertEqual(len(obtener_cupos_donante("12.345.678-9", db_path=self.db_path)), 1)

        # 4. Liberar de nuevo debe restituir el stock a 10 y dejar el cupo en LIBERADO
        l2 = liberar_cupo("saga-ciclo", db_path=self.db_path)
        self.assertFalse(l2["idempotente"])
        self.assertEqual(l2["estado"], "LIBERADO")
        self.assertEqual(self._stock_disponible("JORNADA-01"), 10)
        self.assertEqual(len(obtener_cupos_donante("12.345.678-9", db_path=self.db_path)), 0)

    def test_concurrencia_jornada_unica(self) -> None:
        """Verifica con solicitudes simultáneas sobre JORNADA-UNICA que solo 1 gana el cupo y el stock no queda negativo."""
        total_hilos = 8
        barrera = threading.Barrier(total_hilos)

        def intentar_reserva(indice: int) -> str:
            barrera.wait()
            try:
                reservar_cupo(
                    rut_donante=f"10.000.00{indice}-{indice}",
                    id_jornada="JORNADA-UNICA",
                    horario="09:00",
                    id_saga=f"saga-conc-{indice}",
                    db_path=self.db_path,
                )
                return "OK"
            except JornadaSinCuposError:
                return "SIN_CUPOS"

        with ThreadPoolExecutor(max_workers=total_hilos) as pool:
            resultados = list(pool.map(intentar_reserva, range(total_hilos)))

        self.assertEqual(resultados.count("OK"), 1)
        self.assertEqual(resultados.count("SIN_CUPOS"), total_hilos - 1)
        self.assertEqual(self._stock_disponible("JORNADA-UNICA"), 0)

    def test_liberar_antes_de_reservar_rechaza_reserva_posterior(self) -> None:
        """Si la compensación llega antes que la reserva, liberar responde 404 y la reserva posterior se rechaza (409)."""
        with self.assertRaises(ReservaNoEncontradaError):
            liberar_cupo("saga-fuera-de-orden", db_path=self.db_path)

        with self.assertRaises(SagaCanceladaError):
            reservar_cupo("12.345.678-9", "JORNADA-01", "09:00", id_saga="saga-fuera-de-orden", db_path=self.db_path)

        self.assertEqual(self._stock_disponible("JORNADA-01"), 10)
        self.assertEqual(len(obtener_cupos_donante("12.345.678-9", db_path=self.db_path)), 0)


if __name__ == "__main__":
    unittest.main()

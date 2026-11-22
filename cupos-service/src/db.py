import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("cupos-service")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = str(BASE_DIR / "data" / "cupos.db")
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"

class JornadaSinCuposError(Exception):
    """Jornada sin cupos disponibles HTTP 409"""

class ColisionIdempotenciaError(Exception):
    """ID saga o cupo ya existe con otros datos HTTP 409"""

class SagaCanceladaError(Exception):
    """La SAGA fue cancelada previamente por compensación HTTP 409"""

class ReservaNoEncontradaError(Exception):
    """Reserva para liberar no existe HTTP 404"""

class JornadaNoEncontradaError(Exception):
    """Jornada solicitada no existe HTTP 404"""

def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """SQL en modo WAL y control manual de transacciones"""
    target = Path(db_path or os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target), timeout=2.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in ("synchronous = NORMAL", "foreign_keys = ON", "busy_timeout = 2000"):
        conn.execute(f"PRAGMA {pragma};")
    return conn

@contextmanager
def immediate_transaction(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Ejecuta una transacción atómica con BEGIN IMMEDIATE evitando condiciones de carrera"""
    conn = get_connection(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE;")
        yield conn
        conn.execute("COMMIT;")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK;")
        raise
    finally:
        conn.close()

def init_db(db_path: Optional[str] = None) -> None:
    """Inicializa en modo WAL"""
    conn = get_connection(db_path)
    try:
        if SCHEMA_PATH.exists():
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute("PRAGMA journal_mode = WAL;")
            logger.info("[CUPOS] SQLite inicializado en modo WAL")
    finally:
        conn.close()

"""Función para todos los logs"""
def _registrar_log(conn: sqlite3.Connection, id_cupo: str, id_saga: str,
    rut: str, jornada: str, horario: str, accion: str, detalle: str,) -> None:
    conn.execute(
        "INSERT INTO log_cupos (id_cupo, id_saga, rut_donante, id_jornada, horario, accion, detalle) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (id_cupo, id_saga, rut, jornada, horario, accion, detalle),
    )

def reservar_cupo(rut_donante: str, id_jornada: str, horario: str, id_saga: str,
    db_path: Optional[str] = None,) -> Dict[str, Any]:
    """
    Reserva un cupo de forma atómica e idempotente usando BEGIN IMMEDIATE.
    Contrato de compensación: si el id_saga fue compensado (sagas_canceladas o estado LIBERADO),
    queda cerrado definitivamente y cualquier reintento es rechazado con HTTP 409.
    """
    try:
        with immediate_transaction(db_path) as conn:
            cancelada = conn.execute("SELECT 1 FROM sagas_canceladas WHERE id_saga = ?", (id_saga,)).fetchone()
            if cancelada is not None:
                logger.warning("[CUPOS] RESERVA RECHAZADA POR SAGA CANCELADA (409) | id_saga=%s", id_saga)
                raise SagaCanceladaError("La SAGA ya fue cancelada previamente por compensación.")

            existente = conn.execute("SELECT * FROM cupos WHERE id_saga = ?", (id_saga,)).fetchone()

            # Si la reserva ya existe
            if existente is not None:
                if existente["estado"] == "LIBERADO":
                    logger.warning("[CUPOS] RESERVA RECHAZADA POR SAGA COMPENSADA (409) | id_saga=%s", id_saga)
                    raise SagaCanceladaError("La SAGA ya fue cancelada previamente por compensación.")

                mismos_datos = (existente["rut_donante"] == rut_donante and existente["id_jornada"] == id_jornada
                    and existente["horario"] == horario)
                
                if not mismos_datos:
                    logger.warning("[CUPOS] COLISION IDEMPOTENCIA (409) | id_saga=%s", id_saga)
                    raise ColisionIdempotenciaError("El identificador ya pertenece a otra reserva con datos distintos")

                if existente["estado"] == "RESERVADO":
                    _registrar_log(
                        conn, existente["id_cupo"], id_saga, rut_donante,
                        id_jornada, horario, "RESERVAR_IDEMPOTENTE", "Reintento idempotente de reserva"
                    )
                    logger.info("[CUPOS] RESERVA IDEMPOTENTE (200) | cupo_id=%s | saga=%s", existente["id_cupo"], id_saga)
                    return {
                        "ok": True,
                        "cupo_id": existente["id_cupo"],
                        "id_saga": id_saga,
                        "id_jornada": id_jornada,
                        "horario": horario,
                        "estado": "RESERVADO",
                        "idempotente": True,
                    }

            # Validar existencia y stock disponible en la jornada
            jornada = conn.execute("SELECT * FROM jornadas WHERE id_jornada = ?", (id_jornada,)).fetchone()
            if jornada is None:
                logger.warning("[CUPOS] JORNADA NO ENCONTRADA (404) | jornada=%s", id_jornada)
                raise JornadaNoEncontradaError(f"La jornada '{id_jornada}' no existe.")

            if jornada["cupos_disponibles"] <= 0:
                logger.warning("[CUPOS] RECHAZO SIN CUPOS (409) | jornada=%s", id_jornada)
                raise JornadaSinCuposError(f"La jornada '{id_jornada}' no dispone de cupos disponibles.")

            # Descontar stock y guardar reserva
            conn.execute(
                "UPDATE jornadas SET cupos_disponibles = cupos_disponibles - 1 WHERE id_jornada = ?",
                (id_jornada,),
            )

            nuevo_id = f"cupo-{uuid.uuid4().hex[:12]}"
            conn.execute(
                "INSERT INTO cupos (id_cupo, id_saga, rut_donante, id_jornada, horario, estado) "
                "VALUES (?, ?, ?, ?, ?, 'RESERVADO')",
                (nuevo_id, id_saga, rut_donante, id_jornada, horario),
            )

            _registrar_log(conn, nuevo_id, id_saga, rut_donante, id_jornada, horario, "RESERVAR", "Reserva confirmada")
            logger.info(
                "[CUPOS] RESERVA CREADA (201) | cupo_id=%s | saga=%s | disponibles=%d/%d",
                nuevo_id, id_saga, jornada["cupos_disponibles"] - 1, jornada["capacidad_total"],
            )
            return {
                "ok": True,
                "cupo_id": nuevo_id,
                "id_saga": id_saga,
                "id_jornada": id_jornada,
                "horario": horario,
                "estado": "RESERVADO",
                "idempotente": False,
            }
    except sqlite3.IntegrityError as exc:
        logger.warning("[CUPOS] Violación de integridad SQLite (409): %s", exc)
        raise ColisionIdempotenciaError("Conflicto de integridad en identificador de cupo o SAGA.") from exc

def liberar_cupo(id_saga: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Libera un cupo reservado"""
    with immediate_transaction(db_path) as conn:
        reserva = conn.execute("SELECT * FROM cupos WHERE id_saga = ?", (id_saga,)).fetchone()
        if reserva is None:
            conn.execute("INSERT OR IGNORE INTO sagas_canceladas (id_saga) VALUES (?)", (id_saga,))
            conn.execute("COMMIT;")
            logger.warning("[CUPOS] RESERVA NO ENCONTRADA (404) | saga=%s (marcada como cancelada)", id_saga)
            raise ReservaNoEncontradaError("No se encontró ninguna reserva asociada para liberar.")

        id_cupo_actual = reserva["id_cupo"]
        jornada_actual = reserva["id_jornada"]

        # Verificar idempotencia mediante el estado actual protegido por BEGIN IMMEDIATE
        if reserva["estado"] == "LIBERADO":
            _registrar_log(
                conn, id_cupo_actual, id_saga, reserva["rut_donante"],
                jornada_actual, reserva["horario"], "LIBERAR_IDEMPOTENTE", "Compensación ya aplicada previamente"
            )
            logger.info("[CUPOS] COMPENSACION IDEMPOTENTE (200) | cupo_id=%s | saga=%s", id_cupo_actual, id_saga)
            return {
                "ok": True,
                "cupo_id": id_cupo_actual,
                "id_saga": id_saga,
                "estado": "LIBERADO",
                "idempotente": True,
                "mensaje": "El cupo ya había sido liberado previamente (operación idempotente).",
            }

        # Revertir estado y restituir cupo en la jornada
        conn.execute("UPDATE cupos SET estado = 'LIBERADO', updated_at = datetime('now') WHERE id_cupo = ?", (id_cupo_actual,))
        conn.execute(
            "UPDATE jornadas SET cupos_disponibles = MIN(capacidad_total, cupos_disponibles + 1) WHERE id_jornada = ?",
            (jornada_actual,),
        )
        _registrar_log(
            conn, id_cupo_actual, id_saga, reserva["rut_donante"],
            jornada_actual, reserva["horario"], "LIBERAR", "Compensación SAGA: cupo restituido"
        )
        logger.info("[CUPOS] COMPENSACION EJECUTADA (200) | cupo_id=%s | saga=%s", id_cupo_actual, id_saga)
        return {
            "ok": True,
            "cupo_id": id_cupo_actual,
            "id_saga": id_saga,
            "estado": "LIBERADO",
            "idempotente": False,
            "mensaje": "Cupo liberado exitosamente.",
        }

def obtener_cupos_donante(rut_donante: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retorna la lista de cupos reservados activos de un donante"""
    conn = get_connection(db_path)
    try:
        rows = conn.execute("SELECT id_cupo, id_jornada, horario, estado FROM cupos "
        "WHERE rut_donante = ? AND estado = 'RESERVADO' ORDER BY created_at DESC",(rut_donante,),).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

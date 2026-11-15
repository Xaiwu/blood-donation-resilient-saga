import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("registro-service")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = str(BASE_DIR / "data" / "registro.db")
SCHEMA_PATH = BASE_DIR / "db" / "schema.sql"


class ColisionIdempotenciaError(Exception):
    """ID de SAGA ya existe con datos distintos (HTTP 409)."""


class CitaNoEncontradaError(Exception):
    """La cita solicitada para anular no existe (HTTP 404)."""


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Abre conexión SQLite en modo WAL y con control manual de transacciones."""
    target = Path(db_path or os.getenv("DATABASE_PATH", DEFAULT_DB_PATH)).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(target), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    for pragma in ("journal_mode = WAL", "synchronous = NORMAL", "foreign_keys = ON", "busy_timeout = 5000"):
        conn.execute(f"PRAGMA {pragma};")
    return conn


@contextmanager
def immediate_transaction(db_path: Optional[str] = None) -> Generator[sqlite3.Connection, None, None]:
    """Ejecuta una transacción atómica con BEGIN IMMEDIATE evitando condiciones de carrera."""
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
    """Inicializa el esquema de base de datos en modo WAL."""
    conn = get_connection(db_path)
    try:
        if SCHEMA_PATH.exists():
            conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            conn.execute("PRAGMA journal_mode = WAL;")
            logger.info("[REGISTRO] SQLite inicializado en modo WAL")
    finally:
        conn.close()


def _registrar_log(conn: sqlite3.Connection, id_cita: str, id_saga: str, rut: str, accion: str, detalle: str) -> None:
    conn.execute(
        "INSERT INTO log_registro (id_cita, id_saga, rut_donante, accion, detalle) VALUES (?, ?, ?, ?, ?)",
        (id_cita, id_saga, rut, accion, detalle),
    )


def registrar_donacion(
    id_saga: str,
    rut_donante: str,
    nombre_donante: str = "Donante",
    id_jornada: str = "JORNADA-GENERAL",
    horario: str = "09:00",
    estado_elegibilidad: str = "APTO",
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Registra una cita médica de donación de forma atómica e idempotente."""
    estado_cita = "PENDIENTE_VALIDACION" if estado_elegibilidad == "PENDIENTE_VALIDACION" else "CONFIRMADA"

    try:
        with immediate_transaction(db_path) as conn:
            existente = conn.execute("SELECT * FROM citas_donacion WHERE id_saga = ?", (id_saga,)).fetchone()

            if existente is not None:
                mismos_datos = (
                    existente["rut_donante"] == rut_donante
                    and existente["id_jornada"] == id_jornada
                    and existente["horario"] == horario
                    and existente["estado_elegibilidad"] == estado_elegibilidad
                )
                if not mismos_datos:
                    logger.warning("[REGISTRO] COLISION IDEMPOTENCIA (409) | id_saga=%s", id_saga)
                    raise ColisionIdempotenciaError("El id_saga ya pertenece a otra cita con datos distintos.")

                if existente["estado"] != "ANULADA":
                    _registrar_log(
                        conn, existente["id_cita"], id_saga, rut_donante,
                        "REGISTRAR_IDEMPOTENTE", "Reintento idempotente de registro",
                    )
                    logger.info("[REGISTRO] CITA IDEMPOTENTE (200) | cita_id=%s | saga=%s", existente["id_cita"], id_saga)
                    return {
                        "ok": True,
                        "cita_id": existente["id_cita"],
                        "id_saga": id_saga,
                        "estado_elegibilidad": existente["estado_elegibilidad"],
                        "estado": existente["estado"],
                        "idempotente": True,
                    }

                cita_id = existente["id_cita"]
                conn.execute(
                    "UPDATE citas_donacion SET nombre_donante = ?, estado_elegibilidad = ?, "
                    "estado = ?, updated_at = datetime('now') WHERE id_cita = ?",
                    (nombre_donante, estado_elegibilidad, estado_cita, cita_id),
                )
            else:
                cita_id = f"cita-{uuid.uuid4().hex[:12]}"
                conn.execute(
                    "INSERT INTO citas_donacion (id_cita, id_saga, rut_donante, nombre_donante, "
                    "id_jornada, horario, estado_elegibilidad, estado) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (cita_id, id_saga, rut_donante, nombre_donante, id_jornada, horario, estado_elegibilidad, estado_cita),
                )

            _registrar_log(conn, cita_id, id_saga, rut_donante, "REGISTRAR", f"Cita registrada ({estado_cita})")
            logger.info("[REGISTRO] CITA CREADA (201) | cita_id=%s | saga=%s | estado=%s", cita_id, id_saga, estado_cita)
            return {
                "ok": True,
                "cita_id": cita_id,
                "id_saga": id_saga,
                "estado_elegibilidad": estado_elegibilidad,
                "estado": estado_cita,
                "idempotente": False,
            }
    except sqlite3.IntegrityError as exc:
        logger.warning("[REGISTRO] Violación de integridad SQLite (409): %s", exc)
        raise ColisionIdempotenciaError("Conflicto de integridad en datos de la cita o SAGA.") from exc


def anular_donacion(id_saga: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Anula una cita médica de forma idempotente como compensación del Paso 3."""
    with immediate_transaction(db_path) as conn:
        cita = conn.execute("SELECT * FROM citas_donacion WHERE id_saga = ?", (id_saga,)).fetchone()
        if cita is None:
            logger.warning("[REGISTRO] CITA NO ENCONTRADA PARA ANULAR (404) | saga=%s", id_saga)
            raise CitaNoEncontradaError("No se encontró ninguna cita registrada para el id_saga indicado.")

        id_cita_actual = cita["id_cita"]
        rut_actual = cita["rut_donante"]

        if cita["estado"] == "ANULADA":
            _registrar_log(
                conn, id_cita_actual, id_saga, rut_actual,
                "ANULAR_IDEMPOTENTE", "Compensación ya aplicada previamente",
            )
            logger.info("[REGISTRO] ANULACION IDEMPOTENTE (200) | cita_id=%s | saga=%s", id_cita_actual, id_saga)
            return {
                "ok": True,
                "cita_id": id_cita_actual,
                "id_saga": id_saga,
                "estado": "ANULADA",
                "idempotente": True,
                "mensaje": "La cita médica ya había sido anulada previamente (operación idempotente).",
            }

        conn.execute(
            "UPDATE citas_donacion SET estado = 'ANULADA', updated_at = datetime('now') WHERE id_cita = ?",
            (id_cita_actual,),
        )
        _registrar_log(conn, id_cita_actual, id_saga, rut_actual, "ANULAR", "Compensación SAGA: cita médica anulada")
        logger.info("[REGISTRO] COMPENSACION EJECUTADA (200) | cita_id=%s | saga=%s", id_cita_actual, id_saga)
        return {
            "ok": True,
            "cita_id": id_cita_actual,
            "id_saga": id_saga,
            "estado": "ANULADA",
            "idempotente": False,
            "mensaje": "Cita médica anulada exitosamente.",
        }


def obtener_citas_donante(rut_donante: str, db_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Retorna el historial de citas médicas activas de un donante por su RUT."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT id_cita AS cita_id, rut_donante, nombre_donante, id_jornada, "
            "horario, estado_elegibilidad, estado FROM citas_donacion "
            "WHERE rut_donante = ? AND estado != 'ANULADA' ORDER BY created_at DESC",
            (rut_donante,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()

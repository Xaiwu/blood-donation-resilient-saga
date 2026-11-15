-- WAL y transacciones BEGIN IMMEDIATE para persistencia aislada de citas médicas
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- Tabla de citas médicas de donación agendadas
CREATE TABLE IF NOT EXISTS citas_donacion (
    id_cita TEXT PRIMARY KEY,
    id_saga TEXT NOT NULL UNIQUE,
    rut_donante TEXT NOT NULL,
    nombre_donante TEXT NOT NULL,
    id_jornada TEXT NOT NULL,
    horario TEXT NOT NULL,
    email_donante TEXT,
    telefono_donante TEXT,
    edad INTEGER,
    peso_kg REAL,
    grupo_sanguineo TEXT,
    estado_elegibilidad TEXT NOT NULL DEFAULT 'APTO',
    estado TEXT NOT NULL CHECK (estado IN ('CONFIRMADA', 'PENDIENTE_VALIDACION', 'ANULADA')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Tabla de bitácora de auditoría e idempotencia sobre citas médicas
CREATE TABLE IF NOT EXISTS log_registro (
    id_log INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cita TEXT NOT NULL,
    id_saga TEXT NOT NULL,
    rut_donante TEXT NOT NULL,
    accion TEXT NOT NULL CHECK (accion IN ('REGISTRAR', 'REGISTRAR_IDEMPOTENTE', 'ANULAR', 'ANULAR_IDEMPOTENTE')),
    detalle TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

--  WAL y transacciones BEGIN IMMEDIATE
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- Tabla de jornadas
CREATE TABLE IF NOT EXISTS jornadas (
    id_jornada TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    fecha TEXT NOT NULL,
    capacidad_total INTEGER NOT NULL CHECK (capacidad_total >= 0),
    cupos_disponibles INTEGER NOT NULL CHECK (cupos_disponibles >= 0 AND cupos_disponibles <= capacidad_total),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Tabla de cupos
CREATE TABLE IF NOT EXISTS cupos (
    id_cupo TEXT PRIMARY KEY,
    id_saga TEXT NOT NULL UNIQUE,
    rut_donante TEXT NOT NULL,
    id_jornada TEXT NOT NULL,
    horario TEXT NOT NULL,
    estado TEXT NOT NULL CHECK (estado IN ('RESERVADO', 'LIBERADO')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (id_jornada) REFERENCES jornadas(id_jornada)
);

-- Tabla de log de cupos que permite guardar un registro de cada operación sobre un cupo
CREATE TABLE IF NOT EXISTS log_cupos (
    id_log INTEGER PRIMARY KEY AUTOINCREMENT,
    id_cupo TEXT NOT NULL,
    id_saga TEXT NOT NULL,
    rut_donante TEXT NOT NULL,
    id_jornada TEXT NOT NULL,
    horario TEXT NOT NULL,
    accion TEXT NOT NULL CHECK (accion IN ('RESERVAR', 'LIBERAR', 'LIBERAR_IDEMPOTENTE', 'RESERVAR_IDEMPOTENTE')),
    detalle TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

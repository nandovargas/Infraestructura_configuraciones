"""Base de datos de la plataforma (SQLite en modo WAL).

Guarda planes, tenants, operaciones, despliegues y eventos. NUNCA guarda secretos:
contraseñas, claves SSH y tokens viven solo en el estado cifrado de Terraform.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import config

ESQUEMA = """
CREATE TABLE IF NOT EXISTS planes (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    descripcion TEXT NOT NULL DEFAULT '',
    tipo_frontend TEXT NOT NULL,
    tipo_backend TEXT NOT NULL,
    tipo_bd TEXT NOT NULL,
    disco_app_gb INTEGER NOT NULL,
    disco_bd_gb INTEGER NOT NULL,
    max_estudiantes INTEGER NOT NULL,
    gunicorn_threads INTEGER NOT NULL,
    retencion_dias INTEGER NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    contacto_nombre TEXT NOT NULL,
    contacto_email TEXT NOT NULL,
    contacto_telefono TEXT NOT NULL DEFAULT '',
    entorno TEXT NOT NULL,
    plan_id TEXT NOT NULL REFERENCES planes(id),
    motor TEXT NOT NULL,
    estado TEXT NOT NULL,
    backend_estado TEXT NOT NULL,          -- JSON: dónde vive su estado de Terraform
    salidas TEXT NOT NULL DEFAULT '{}',    -- JSON: salidas NO sensibles (URL, IDs)
    ultimo_error TEXT NOT NULL DEFAULT '',
    creado TEXT NOT NULL,
    actualizado TEXT NOT NULL,
    eliminado_en TEXT
);

CREATE TABLE IF NOT EXISTS operaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    objetivo TEXT NOT NULL,                -- tenant | monitoreo | estado | plataforma
    tenant_id TEXT,
    tipo TEXT NOT NULL,
    estado TEXT NOT NULL,                  -- en_cola | en_progreso | exitosa | fallida
    paso TEXT NOT NULL DEFAULT '',
    progreso INTEGER NOT NULL DEFAULT 0,
    parametros TEXT NOT NULL DEFAULT '{}',
    resultado TEXT NOT NULL DEFAULT '{}',
    intento INTEGER NOT NULL DEFAULT 1,
    reintento_de INTEGER,
    error_resumen TEXT NOT NULL DEFAULT '',
    error_detalle TEXT NOT NULL DEFAULT '',
    log_path TEXT NOT NULL DEFAULT '',
    worker_pid INTEGER,
    creado TEXT NOT NULL,
    iniciado TEXT,
    terminado TEXT
);
CREATE INDEX IF NOT EXISTS ix_op_estado ON operaciones(estado);
CREATE INDEX IF NOT EXISTS ix_op_tenant ON operaciones(tenant_id);

CREATE TABLE IF NOT EXISTS despliegues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operacion_id INTEGER NOT NULL REFERENCES operaciones(id),
    objetivo TEXT NOT NULL,
    tenant_id TEXT,
    tipo TEXT NOT NULL,
    resultado TEXT NOT NULL,
    version_backend TEXT NOT NULL DEFAULT '',
    version_frontend TEXT NOT NULL DEFAULT '',
    version_plantillas TEXT NOT NULL DEFAULT '',
    herramientas TEXT NOT NULL DEFAULT '{}',
    amis TEXT NOT NULL DEFAULT '{}',
    plan TEXT NOT NULL DEFAULT '{}',
    creado TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    origen TEXT NOT NULL,                  -- alerta | operacion | plataforma
    tenant_id TEXT,
    entorno TEXT,
    severidad TEXT NOT NULL,               -- critical | warning | info
    titulo TEXT NOT NULL,
    detalle TEXT NOT NULL DEFAULT '',
    huella TEXT,                           -- identifica una alerta mientras está activa
    estado TEXT NOT NULL,                  -- activa | resuelta | info
    detectado_en TEXT NOT NULL,
    resuelto_en TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_fecha ON eventos(detectado_en);
CREATE UNIQUE INDEX IF NOT EXISTS ux_ev_huella_activa ON eventos(huella) WHERE estado = 'activa';

CREATE TABLE IF NOT EXISTS escaneos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fuente TEXT NOT NULL,                  -- hcl (Terraform y OpenTofu) | pulumi
    stack TEXT NOT NULL,                   -- tenant | monitoreo | estado
    estado TEXT NOT NULL,                  -- en_progreso | exitoso | fallido
    huella TEXT NOT NULL,                  -- huella del código analizado (para saber si cambió)
    version_kics TEXT NOT NULL DEFAULT '',
    archivos INTEGER NOT NULL DEFAULT 0,
    lineas INTEGER NOT NULL DEFAULT 0,
    reglas INTEGER NOT NULL DEFAULT 0,
    severidades TEXT NOT NULL DEFAULT '{}', -- JSON: hallazgos por severidad
    total INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    duracion_s REAL NOT NULL DEFAULT 0,
    usuario TEXT NOT NULL DEFAULT '',
    creado TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_esc_fuente ON escaneos(fuente, stack, id);

CREATE TABLE IF NOT EXISTS config (
    clave TEXT PRIMARY KEY,
    valor TEXT NOT NULL
);
"""


def ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    return con


@contextmanager
def transaccion():
    con = conectar()
    try:
        con.execute("BEGIN IMMEDIATE")
        yield con
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def consultar(sql, params=()):
    con = conectar()
    try:
        return [dict(r) for r in con.execute(sql, params).fetchall()]
    finally:
        con.close()


def uno(sql, params=()):
    filas = consultar(sql, params)
    return filas[0] if filas else None


def ejecutar(sql, params=()):
    con = conectar()
    try:
        cur = con.execute(sql, params)
        return cur.lastrowid, cur.rowcount
    finally:
        con.close()


def get_config(clave, defecto=None):
    fila = uno("SELECT valor FROM config WHERE clave = ?", (clave,))
    return json.loads(fila["valor"]) if fila else defecto


def set_config(clave, valor):
    ejecutar("INSERT INTO config(clave, valor) VALUES(?, ?) "
             "ON CONFLICT(clave) DO UPDATE SET valor = excluded.valor", (clave, json.dumps(valor)))


PLANES_INICIALES = [
    dict(id="basico", nombre="Básico",
         descripcion="Instituciones pequeñas o pilotos. Una máquina pequeña por capa.",
         tipo_frontend="t3.micro", tipo_backend="t3.micro", tipo_bd="t3.micro",
         disco_app_gb=16, disco_bd_gb=20, max_estudiantes=300, gunicorn_threads=4, retencion_dias=7),
    dict(id="estandar", nombre="Estándar",
         descripcion="Colegios e institutos medianos. Más memoria para la API y la base de datos.",
         tipo_frontend="t3.micro", tipo_backend="t3.small", tipo_bd="t3.small",
         disco_app_gb=20, disco_bd_gb=40, max_estudiantes=1500, gunicorn_threads=8, retencion_dias=15),
    dict(id="avanzado", nombre="Avanzado",
         descripcion="Universidades o varias sedes. Mayor cómputo, disco y retención.",
         tipo_frontend="t3.small", tipo_backend="t3.medium", tipo_bd="t3.medium",
         disco_app_gb=30, disco_bd_gb=100, max_estudiantes=10000, gunicorn_threads=16, retencion_dias=30),
]


def inicializar():
    con = conectar()
    try:
        con.executescript(ESQUEMA)
        if not con.execute("SELECT 1 FROM planes LIMIT 1").fetchone():
            t = ahora()
            for p in PLANES_INICIALES:
                con.execute(
                    "INSERT INTO planes(id, nombre, descripcion, tipo_frontend, tipo_backend, tipo_bd, disco_app_gb,"
                    " disco_bd_gb, max_estudiantes, gunicorn_threads, retencion_dias, activo, creado, actualizado)"
                    " VALUES(:id, :nombre, :descripcion, :tipo_frontend, :tipo_backend, :tipo_bd, :disco_app_gb,"
                    " :disco_bd_gb, :max_estudiantes, :gunicorn_threads, :retencion_dias, 1, :t, :t)",
                    {**p, "t": t})
    finally:
        con.close()

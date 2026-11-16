"""Reglas de negocio de la plataforma: planes, tenants y cola de operaciones.

La interfaz (Streamlit) solo llama a estas funciones; el worker ejecuta las operaciones.
"""
import json
import re

from . import config, db

# ---------- Estados ----------
ESTADOS_TENANT = {
    "pendiente": "⏳ Pendiente",
    "aprovisionando": "🔧 Aprovisionando",
    "activo": "🟢 Activo",
    "error": "🔴 Error",
    "suspendiendo": "⏸️ Suspendiendo",
    "suspendido": "💤 Suspendido",
    "reanudando": "▶️ Reanudando",
    "eliminando": "🗑️ Eliminando",
    "eliminado": "⚫ Eliminado",
}
ESTADOS_OPERACION = {"en_cola": "⏳ En cola", "en_progreso": "🔄 En progreso",
                     "exitosa": "✅ Exitosa", "fallida": "❌ Fallida"}

# Acciones válidas según el estado del tenant
ACCIONES = {
    "pendiente": {"desplegar", "eliminar"},
    "activo": {"desplegar", "suspender", "eliminar"},
    "error": {"desplegar", "suspender", "reanudar", "eliminar"},
    "suspendido": {"reanudar", "eliminar"},
}
# Estado mientras corre cada operación y al terminar bien
TRANSICION = {
    "desplegar": ("aprovisionando", "activo"),
    "suspender": ("suspendiendo", "suspendido"),
    "reanudar": ("reanudando", "activo"),
    "eliminar": ("eliminando", "eliminado"),
}

TIPOS_OPERACION = {
    "desplegar": "Desplegar / actualizar tenant",
    "suspender": "Suspender tenant",
    "reanudar": "Reanudar tenant",
    "eliminar": "Eliminar tenant",
    "desplegar_monitoreo": "Desplegar monitoreo (Grafana)",
    "eliminar_monitoreo": "Eliminar monitoreo",
    "preparar_estado": "Preparar almacenamiento del estado (S3)",
}

# Precio aproximado on-demand us-east-1 (USD/hora) para estimar el costo mensual
PRECIO_HORA = {"t3.micro": 0.0104, "t3.small": 0.0208, "t3.medium": 0.0416, "t3.large": 0.0832,
               "t3.xlarge": 0.1664}
PRECIO_GB_GP3 = 0.08
PRECIO_IPV4_HORA = 0.005
TIPOS_INSTANCIA = list(PRECIO_HORA)

RE_ID = re.compile(r"^[a-z][a-z0-9-]{2,20}$")
RE_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ErrorValidacion(Exception):
    pass


# ---------- Planes ----------
def listar_planes(solo_activos=False):
    sql = "SELECT * FROM planes" + (" WHERE activo = 1" if solo_activos else "") + " ORDER BY disco_bd_gb"
    return db.consultar(sql)


def obtener_plan(plan_id):
    return db.uno("SELECT * FROM planes WHERE id = ?", (plan_id,))


def estimar_costo(plan) -> float:
    horas = 730
    computo = sum(PRECIO_HORA.get(plan[k], 0) for k in ("tipo_frontend", "tipo_backend", "tipo_bd")) * horas
    discos = (plan["disco_app_gb"] * 2 + plan["disco_bd_gb"]) * PRECIO_GB_GP3
    ips = 3 * PRECIO_IPV4_HORA * horas
    return round(computo + discos + ips, 2)


def costo_suspendido(plan) -> float:
    """Suspendido: no se paga cómputo; sí discos e IP elásticas."""
    return round((plan["disco_app_gb"] * 2 + plan["disco_bd_gb"]) * PRECIO_GB_GP3 + 2 * PRECIO_IPV4_HORA * 730, 2)


def validar_plan(p, nuevo):
    errores = []
    if nuevo and not RE_ID.match(p.get("id", "")):
        errores.append("El identificador del plan debe tener 3-21 caracteres: minúsculas, números y guiones.")
    if nuevo and obtener_plan(p.get("id", "")):
        errores.append("Ya existe un plan con ese identificador.")
    if not p.get("nombre", "").strip():
        errores.append("Indique el nombre del plan.")
    for k in ("tipo_frontend", "tipo_backend", "tipo_bd"):
        if p.get(k) not in PRECIO_HORA:
            errores.append(f"{k}: tipo de instancia no soportado.")
    if int(p.get("disco_app_gb", 0)) < 16 or int(p.get("disco_bd_gb", 0)) < 16:
        errores.append("Los discos deben ser de al menos 16 GB (tamaño de las AMIs).")
    if int(p.get("max_estudiantes", -1)) < 0:
        errores.append("El límite de estudiantes no puede ser negativo (0 = sin límite).")
    if not 1 <= int(p.get("gunicorn_threads", 0)) <= 64:
        errores.append("Los hilos de la API deben estar entre 1 y 64.")
    if not 1 <= int(p.get("retencion_dias", 0)) <= 365:
        errores.append("La retención debe estar entre 1 y 365 días.")
    return errores


def guardar_plan(p, nuevo):
    errores = validar_plan(p, nuevo)
    if errores:
        raise ErrorValidacion("\n".join(errores))
    t = db.ahora()
    campos = ["nombre", "descripcion", "tipo_frontend", "tipo_backend", "tipo_bd", "disco_app_gb",
              "disco_bd_gb", "max_estudiantes", "gunicorn_threads", "retencion_dias", "activo"]
    vals = {k: p[k] for k in campos} | {"id": p["id"], "t": t}
    if nuevo:
        db.ejecutar(f"INSERT INTO planes(id, {', '.join(campos)}, creado, actualizado) "
                    f"VALUES(:id, {', '.join(':' + c for c in campos)}, :t, :t)", vals)
    else:
        db.ejecutar(f"UPDATE planes SET {', '.join(f'{c} = :{c}' for c in campos)}, actualizado = :t WHERE id = :id", vals)


def tenants_con_plan(plan_id):
    return db.uno("SELECT count(*) n FROM tenants WHERE plan_id = ? AND estado != 'eliminado'", (plan_id,))["n"]


# ---------- Dónde vive el estado de Terraform ----------
def backend_para(objetivo, ident):
    """Configuración del backend de estado para un stack nuevo (se fija al crearlo)."""
    tipo = db.get_config("estado_tipo", "local")
    if tipo == "s3":
        return {"tipo": "s3", "bucket": db.get_config("estado_bucket"), "region": config.REGION,
                "key": f"{objetivo}/{ident}/terraform.tfstate"}
    ruta = (config.ESTADO_LOCAL / objetivo / f"{ident}.tfstate").as_posix()
    return {"tipo": "local", "path": ruta}


# ---------- Tenants ----------
def listar_tenants(texto="", estados=None, planes=None, entornos=None, incluir_eliminados=False):
    sql, params = "SELECT t.*, p.nombre AS plan_nombre FROM tenants t JOIN planes p ON p.id = t.plan_id WHERE 1=1", []
    if texto:
        sql += " AND (lower(t.nombre) LIKE ? OR t.id LIKE ?)"
        params += [f"%{texto.lower()}%", f"%{texto.lower()}%"]
    for campo, valores in (("t.estado", estados), ("t.plan_id", planes), ("t.entorno", entornos)):
        if valores:
            sql += f" AND {campo} IN ({','.join('?' * len(valores))})"
            params += list(valores)
    if not incluir_eliminados and not (estados and "eliminado" in estados):
        sql += " AND t.estado != 'eliminado'"
    return db.consultar(sql + " ORDER BY t.creado DESC", params)


def obtener_tenant(tenant_id):
    t = db.uno("SELECT t.*, p.nombre AS plan_nombre FROM tenants t JOIN planes p ON p.id = t.plan_id "
               "WHERE t.id = ?", (tenant_id,))
    if t:
        t["salidas"] = json.loads(t["salidas"] or "{}")
        t["backend_estado"] = json.loads(t["backend_estado"])
    return t


def validar_tenant(d):
    errores = []
    if not RE_ID.match(d.get("id", "")):
        errores.append("El identificador debe tener 3-21 caracteres, empezar con letra y usar solo minúsculas, "
                       "números y guiones (ej. colegio-andino).")
    elif db.uno("SELECT 1 FROM tenants WHERE id = ?", (d["id"],)):
        errores.append(f"El identificador «{d['id']}» ya está en uso (también se reservan los de tenants eliminados).")
    if len(d.get("nombre", "").strip()) < 3:
        errores.append("Indique el nombre de la institución.")
    if not d.get("contacto_nombre", "").strip():
        errores.append("Indique la persona de contacto.")
    if not RE_EMAIL.match(d.get("contacto_email", "")):
        errores.append("El correo de contacto no es válido.")
    if d.get("entorno") not in config.ENTORNOS:
        errores.append("Elija un entorno válido.")
    plan = obtener_plan(d.get("plan_id", ""))
    if not plan or not plan["activo"]:
        errores.append("Elija un plan activo.")
    from motores import MOTORES, para_tenants
    elegibles = para_tenants()
    if d.get("motor") not in elegibles:
        errores.append("Elija un motor de aprovisionamiento disponible: "
                       + ", ".join(c.etiqueta for c in elegibles.values()) + ".")
    elif not MOTORES[d["motor"]].instalado():
        errores.append(f"{MOTORES[d['motor']].etiqueta} no está instalado en la máquina de la plataforma "
                       f"('{MOTORES[d['motor']].binario}' no está en el PATH).")
    return errores


def crear_tenant(d):
    errores = validar_tenant(d)
    if errores:
        raise ErrorValidacion("\n".join(errores))
    t = db.ahora()
    backend = backend_para("tenants", d["id"])
    if backend["tipo"] == "s3" and not backend["bucket"]:
        raise ErrorValidacion("El almacenamiento S3 del estado no está preparado.")
    db.ejecutar(
        "INSERT INTO tenants(id, nombre, contacto_nombre, contacto_email, contacto_telefono, entorno, plan_id, motor,"
        " estado, backend_estado, creado, actualizado) VALUES(?,?,?,?,?,?,?,?, 'pendiente', ?, ?, ?)",
        (d["id"], d["nombre"].strip(), d["contacto_nombre"].strip(), d["contacto_email"].strip(),
         d.get("contacto_telefono", "").strip(), d["entorno"], d["plan_id"], d["motor"], json.dumps(backend), t, t))
    registrar_evento("operacion", f"Tenant creado ({d['plan_id']}, {d['entorno']})", tenant_id=d["id"],
                     entorno=d["entorno"])
    return encolar("tenant", "desplegar", d["id"], {"plan_id": d["plan_id"]})


def acciones_permitidas(tenant):
    if operacion_activa(tenant["id"]):
        return set()
    return ACCIONES.get(tenant["estado"], set())


def solicitar(tenant_id, accion, **parametros):
    t = obtener_tenant(tenant_id)
    if not t:
        raise ErrorValidacion("El tenant no existe.")
    if accion not in acciones_permitidas(t):
        raise ErrorValidacion(f"No se puede «{accion}» un tenant en estado «{t['estado']}» "
                              "o con otra operación en curso.")
    if accion == "desplegar":
        plan = obtener_plan(parametros.get("plan_id") or t["plan_id"])
        actual = obtener_plan(t["plan_id"])
        if plan["id"] != actual["id"] and t["estado"] != "pendiente":
            if plan["disco_app_gb"] < actual["disco_app_gb"] or plan["disco_bd_gb"] < actual["disco_bd_gb"]:
                raise ErrorValidacion("No se puede cambiar a un plan con discos más pequeños (EBS no se reduce).")
        parametros["plan_id"] = plan["id"]
    return encolar("tenant", accion, tenant_id, parametros)


# ---------- Operaciones ----------
def operacion_activa(tenant_id=None, objetivo="tenant"):
    if tenant_id:
        return db.uno("SELECT * FROM operaciones WHERE tenant_id = ? AND estado IN ('en_cola','en_progreso')",
                      (tenant_id,))
    return db.uno("SELECT * FROM operaciones WHERE objetivo = ? AND estado IN ('en_cola','en_progreso')",
                  (objetivo,))


def encolar(objetivo, tipo, tenant_id=None, parametros=None, reintento_de=None):
    """Encola una operación. Garantiza UNA operación activa por tenant/objetivo (evita duplicar recursos)."""
    with db.transaccion() as con:
        if tenant_id:
            ocupado = con.execute("SELECT id FROM operaciones WHERE tenant_id = ? AND estado IN "
                                  "('en_cola','en_progreso')", (tenant_id,)).fetchone()
        else:
            ocupado = con.execute("SELECT id FROM operaciones WHERE objetivo = ? AND tenant_id IS NULL AND estado IN "
                                  "('en_cola','en_progreso')", (objetivo,)).fetchone()
        if ocupado:
            raise ErrorValidacion(f"Ya hay una operación en curso (#{ocupado['id']}). Espere a que termine.")
        intento = 1
        if reintento_de:
            previo = con.execute("SELECT intento FROM operaciones WHERE id = ?", (reintento_de,)).fetchone()
            intento = previo["intento"] + 1
        cur = con.execute(
            "INSERT INTO operaciones(objetivo, tenant_id, tipo, estado, parametros, intento, reintento_de, creado)"
            " VALUES(?,?,?, 'en_cola', ?, ?, ?, ?)",
            (objetivo, tenant_id, tipo, json.dumps(parametros or {}), intento, reintento_de, db.ahora()))
        op_id = cur.lastrowid
        con.execute("UPDATE operaciones SET log_path = ? WHERE id = ?",
                    (str(config.LOGS / f"operacion-{op_id}.log"), op_id))
        if objetivo == "tenant" and tipo in TRANSICION:
            con.execute("UPDATE tenants SET estado = ?, actualizado = ? WHERE id = ?",
                        (TRANSICION[tipo][0], db.ahora(), tenant_id))
    return op_id


def reintentar(op_id):
    op = obtener_operacion(op_id)
    if not op or op["estado"] != "fallida":
        raise ErrorValidacion("Solo se pueden reintentar operaciones fallidas.")
    ultima = db.uno("SELECT id FROM operaciones WHERE coalesce(tenant_id,'') = coalesce(?, '') AND objetivo = ? "
                    "ORDER BY id DESC LIMIT 1", (op["tenant_id"], op["objetivo"]))
    if ultima and ultima["id"] != op_id:
        raise ErrorValidacion("Ya hubo operaciones posteriores sobre este recurso; reintente la más reciente.")
    return encolar(op["objetivo"], op["tipo"], op["tenant_id"], op["parametros"], reintento_de=op_id)


def obtener_operacion(op_id):
    op = db.uno("SELECT * FROM operaciones WHERE id = ?", (op_id,))
    if op:
        op["parametros"] = json.loads(op["parametros"])
        op["resultado"] = json.loads(op["resultado"])
    return op


def listar_operaciones(tenant_id=None, estados=None, limite=200):
    sql, params = "SELECT * FROM operaciones WHERE 1=1", []
    if tenant_id:
        sql += " AND tenant_id = ?"
        params.append(tenant_id)
    if estados:
        sql += f" AND estado IN ({','.join('?' * len(estados))})"
        params += list(estados)
    return db.consultar(sql + " ORDER BY id DESC LIMIT ?", params + [limite])


def leer_log(op, ultimas=None):
    try:
        with open(op["log_path"], encoding="utf-8", errors="replace") as f:
            lineas = f.readlines()
        return "".join(lineas[-ultimas:] if ultimas else lineas)
    except (OSError, KeyError):
        return ""


def listar_despliegues(tenant_id=None, limite=50):
    if tenant_id:
        return db.consultar("SELECT * FROM despliegues WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
                            (tenant_id, limite))
    return db.consultar("SELECT * FROM despliegues ORDER BY id DESC LIMIT ?", (limite,))


# ---------- Eventos ----------
def registrar_evento(origen, titulo, detalle="", tenant_id=None, entorno=None, severidad="info"):
    db.ejecutar("INSERT INTO eventos(origen, tenant_id, entorno, severidad, titulo, detalle, estado, detectado_en)"
                " VALUES(?,?,?,?,?,?, 'info', ?)", (origen, tenant_id, entorno, severidad, titulo, detalle, db.ahora()))


def listar_eventos(tenant_id=None, entornos=None, solo_alertas=False, solo_activas=False, limite=100):
    sql, params = "SELECT * FROM eventos WHERE 1=1", []
    if tenant_id:
        sql += " AND tenant_id = ?"
        params.append(tenant_id)
    if entornos:
        sql += f" AND entorno IN ({','.join('?' * len(entornos))})"
        params += list(entornos)
    if solo_alertas:
        sql += " AND origen = 'alerta'"
    if solo_activas:
        sql += " AND estado = 'activa'"
    return db.consultar(sql + " ORDER BY detectado_en DESC LIMIT ?", params + [limite])


def estado_worker():
    latido = db.get_config("worker_latido")
    return latido

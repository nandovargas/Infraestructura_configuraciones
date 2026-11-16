"""Manejadores de las operaciones que ejecuta el worker en segundo plano.

Cada operación es repetible: Terraform converge al estado deseado leyendo su estado,
Ansible es idempotente y los snapshots se crean con nombre/etiquetas propias. Por eso
reintentar una operación fallida no duplica recursos.
"""
import json
import shutil
import time
from datetime import datetime

from motores import obtener as obtener_motor
from nucleo import config, db, servicios, versiones
from nucleo.errores import ErrorOperacion
from nucleo.seguridad import Redactor

from . import ansible, aws


class Contexto:
    """Registro (sin secretos) y progreso de una operación."""

    def __init__(self, op):
        self.op = op
        self.redactor = Redactor()
        self._log = open(op["log_path"], "a", encoding="utf-8")
        self._ultima_escritura = 0.0
        self._rango = (0, 100)

    def log(self, msg: str):
        msg = self.redactor.limpiar(str(msg))
        for linea in msg.splitlines() or [""]:
            self._log.write(f"[{datetime.now():%H:%M:%S}] {linea}\n")
        self._log.flush()

    def paso(self, nombre: str, desde: int, hasta: int):
        self._rango = (desde, hasta)
        self.log(f"══ {nombre} ══")
        self._guardar(desde, nombre, forzar=True)

    def progreso(self, fraccion: float, detalle: str = ""):
        desde, hasta = self._rango
        valor = int(desde + (hasta - desde) * max(0.0, min(fraccion, 1.0)))
        self._guardar(valor, None)

    def _guardar(self, progreso, paso, forzar=False):
        if not forzar and time.monotonic() - self._ultima_escritura < 1:
            return
        self._ultima_escritura = time.monotonic()
        if paso is None:
            db.ejecutar("UPDATE operaciones SET progreso = max(progreso, ?) WHERE id = ?", (progreso, self.op["id"]))
        else:
            db.ejecutar("UPDATE operaciones SET progreso = max(progreso, ?), paso = ? WHERE id = ?",
                        (progreso, paso, self.op["id"]))

    def cerrar(self):
        self._log.close()


# ---------- utilidades ----------
def _motor(nombre, stack, trabajo, backend, ctx):
    return obtener_motor(nombre, stack=stack, trabajo=trabajo, backend=backend, log=ctx.log)


def _motor_tenant(t, ctx):
    return _motor(t["motor"], "tenant", config.TRABAJO / "tenants" / t["id"], t["backend_estado"], ctx)


def _variables_tenant(t, plan, estado, admin_cidr):
    return {"region": config.REGION, "tenant_id": t["id"], "entorno": t["entorno"], "plan": plan["id"],
            "tipo_frontend": plan["tipo_frontend"], "tipo_backend": plan["tipo_backend"], "tipo_bd": plan["tipo_bd"],
            "disco_app_gb": plan["disco_app_gb"], "disco_bd_gb": plan["disco_bd_gb"],
            "estado": estado, "admin_cidr": admin_cidr, "dominio_base": config.DOMINIO_TENANTS,
            "gestion": t["motor"]}


def _requisitos(ctx, necesita_docker=True):
    ident = aws.identidad()
    ctx.log(f"Cuenta AWS ****{ident['cuenta'][-4:]} · región {config.REGION}")
    if necesita_docker and not ansible.docker_disponible():
        raise ErrorOperacion("Docker no está en ejecución (error during connect).")
    cidr = aws.mi_cidr()
    ctx.log(f"Acceso administrativo (SSH/Prometheus) permitido desde {cidr}")
    return cidr


def _solo_secretos(secretos):
    """Nombre de la base y de su usuario no son secretos: ocultarlos borraría palabras normales del log."""
    return {k: v for k, v in secretos.items() if k not in ("db_name", "db_user")}


def _guardar_salidas(t, salidas):
    publicas = {k: v for k, v in salidas.items() if k != "secretos"}
    db.ejecutar("UPDATE tenants SET salidas = ?, actualizado = ? WHERE id = ?",
                (json.dumps(publicas), db.ahora(), t["id"]))
    return publicas


def _inventario_tenant(t, plan, salidas, secretos):
    h = salidas["hosts"]
    n = f"asist-{t['id']}-{t['entorno']}"
    grupos = {
        "frontend": [(f"{n}-frontend", {"ansible_host": h["frontend"]["publica"]})],
        "backend": [(f"{n}-backend", {"ansible_host": h["backend"]["publica"], "private_ip": h["backend"]["privada"]})],
        "bd": [(f"{n}-bd", {"ansible_host": h["bd"]["publica"], "private_ip": h["bd"]["privada"]})],
    }
    variables = {
        "ansible_user": "ubuntu", "ansible_python_interpreter": "/usr/bin/python3",
        "frontend_url": salidas.get("frontend_url") or config.url_tenant(t["id"], h["frontend"]["publica"]),
        "frontend_public_ip": h["frontend"]["publica"], "frontend_private_ip": h["frontend"]["privada"],
        "backend_public_ip": h["backend"]["publica"], "backend_private_ip": h["backend"]["privada"],
        "bd_private_ip": h["bd"]["privada"],
        "db_name": secretos["db_name"], "db_user": secretos["db_user"], "db_password": secretos["db_password"],
        "django_secret_key": secretos["django_secret"], "admin_password": secretos["admin_password"],
        "tenant_id": t["id"], "entorno": t["entorno"], "plan": plan["id"],
        "max_estudiantes": plan["max_estudiantes"], "gunicorn_threads": plan["gunicorn_threads"],
    }
    return ansible.inventario_ini(grupos, variables)


def _registrar_despliegue(ctx, objetivo, tenant_id, tipo, motor, stack, amis=None, plan=None):
    herramientas = versiones.herramientas_base() | motor.versiones()
    try:
        herramientas["ansible-core"] = ansible.version_ansible()
    except Exception:
        pass
    db.ejecutar(
        "INSERT INTO despliegues(operacion_id, objetivo, tenant_id, tipo, resultado, version_backend, version_frontend,"
        " version_plantillas, herramientas, amis, plan, creado) VALUES(?,?,?,?, 'exitoso', ?,?,?,?,?,?,?)",
        (ctx.op["id"], objetivo, tenant_id, tipo,
         versiones.version_app(config.APP_BACKEND) if objetivo == "tenant" else "",
         versiones.version_app(config.APP_FRONTEND) if objetivo == "tenant" else "",
         versiones.version_plantillas(stack, motor.plantilla), json.dumps(herramientas), json.dumps(amis or {}),
         json.dumps(plan or {}), db.ahora()))
    ctx.log("Versiones registradas: " + ", ".join(f"{k}={v}" for k, v in herramientas.items()))


# ---------- Tenants ----------
def desplegar(ctx):
    op = ctx.op
    t = servicios.obtener_tenant(op["tenant_id"])
    plan = servicios.obtener_plan(op["parametros"].get("plan_id") or t["plan_id"])

    ctx.paso("Verificando requisitos (AWS, Docker, IP de administración)", 0, 4)
    cidr = _requisitos(ctx)

    motor = _motor_tenant(t, ctx)
    ctx.paso(f"Preparando {motor.etiqueta} (plantilla + estado del tenant)", 4, 10)
    motor.preparar(_variables_tenant(t, plan, "running", cidr))

    ctx.paso("Aprovisionando infraestructura: red, seguridad, 3 instancias, IP fijas", 10, 45)
    resumen = motor.aplicar(ctx.progreso)
    ctx.log(f"Cambios de infraestructura: {resumen}")

    salidas = motor.salidas()
    secretos = salidas.pop("secretos")
    ctx.redactor.agregar(_solo_secretos(secretos))
    publicas = _guardar_salidas(t, salidas)
    db.ejecutar("UPDATE tenants SET plan_id = ? WHERE id = ?", (plan["id"], t["id"]))

    ctx.paso("Configurando y desplegando la aplicación con Ansible", 45, 97)
    ansible.ejecutar_playbook("sitio.yml", _inventario_tenant(t, plan, salidas, secretos), secretos["ssh_privada"],
                              ctx.log, ctx.progreso, total_tareas=80)

    ctx.paso("Registrando versiones del despliegue", 97, 100)
    _registrar_despliegue(ctx, "tenant", t["id"], "desplegar", motor, "tenant", publicas.get("amis"), plan)
    return {"frontend_url": publicas["frontend_url"], "backend_url": publicas["backend_url"]}


def suspender(ctx):
    t = servicios.obtener_tenant(ctx.op["tenant_id"])
    plan = servicios.obtener_plan(t["plan_id"])
    ctx.paso("Verificando requisitos", 0, 5)
    cidr = _requisitos(ctx, necesita_docker=False)

    resultado = {}
    vol = t["salidas"].get("bd_volumen_id")
    if vol:
        ctx.paso(f"Respaldo de la base de datos (retención {plan['retencion_dias']} días)", 5, 45)
        resultado["snapshot"] = aws.crear_snapshot(vol, t["id"], t["entorno"], plan["retencion_dias"],
                                                   "suspension", ctx.log)

    ctx.paso("Deteniendo las instancias (se conservan discos e IP)", 45, 95)
    motor = _motor_tenant(t, ctx)
    motor.preparar(_variables_tenant(t, plan, "stopped", cidr))
    motor.aplicar(ctx.progreso)

    ctx.paso("Actualizando estado", 95, 100)
    motor.refrescar()
    _guardar_salidas(t, {k: v for k, v in motor.salidas().items()})
    return resultado


def reanudar(ctx):
    t = servicios.obtener_tenant(ctx.op["tenant_id"])
    plan = servicios.obtener_plan(t["plan_id"])
    ctx.paso("Verificando requisitos", 0, 5)
    cidr = _requisitos(ctx)

    ctx.paso("Iniciando las instancias", 5, 50)
    motor = _motor_tenant(t, ctx)
    motor.preparar(_variables_tenant(t, plan, "running", cidr))
    motor.aplicar(ctx.progreso)
    motor.refrescar()   # la BD obtiene una IP pública nueva al encenderse
    salidas = motor.salidas()
    secretos = salidas.pop("secretos")
    ctx.redactor.agregar(_solo_secretos(secretos))
    _guardar_salidas(t, salidas)

    ctx.paso("Verificando servicios (frontend, login en la API, métricas)", 50, 100)
    ansible.ejecutar_playbook("sitio.yml", _inventario_tenant(t, plan, salidas, secretos), secretos["ssh_privada"],
                              ctx.log, ctx.progreso, total_tareas=8, extra_args=["--tags", "verificar"])
    return {"frontend_url": salidas["frontend_url"]}


def eliminar(ctx):
    t = servicios.obtener_tenant(ctx.op["tenant_id"])
    plan = servicios.obtener_plan(t["plan_id"])
    conservar = ctx.op["parametros"].get("conservar_snapshot", True)

    ctx.paso("Verificando requisitos", 0, 5)
    cidr = _requisitos(ctx, necesita_docker=False)
    resultado = {}

    vol = t["salidas"].get("bd_volumen_id")
    if conservar and vol:
        ctx.paso(f"Snapshot final de la base de datos (se conserva {plan['retencion_dias']} días)", 5, 40)
        try:
            resultado["snapshot"] = aws.crear_snapshot(vol, t["id"], t["entorno"], plan["retencion_dias"],
                                                       "eliminacion", ctx.log)
        except Exception as e:
            if "InvalidVolume.NotFound" not in str(e):
                raise
            ctx.log("El volumen de BD ya no existe (reintento tras una eliminación parcial): se omite el snapshot.")

    ctx.paso("Eliminando la infraestructura del tenant", 40, 95)
    motor = _motor_tenant(t, ctx)
    motor.preparar(_variables_tenant(t, plan, "running", cidr))
    resultado["recursos"] = motor.destruir(ctx.progreso)

    ctx.paso("Limpiando datos de trabajo", 95, 100)
    shutil.rmtree(config.TRABAJO / "tenants" / t["id"], ignore_errors=True)
    db.ejecutar("UPDATE tenants SET eliminado_en = ?, salidas = '{}' WHERE id = ?", (db.ahora(), t["id"]))
    return resultado


# ---------- Plataforma ----------
def _backend_monitoreo():
    b = db.get_config("monitoreo_backend")
    if not b:
        b = servicios.backend_para("plataforma", "monitoreo")
        db.set_config("monitoreo_backend", b)
    return b


def desplegar_monitoreo(ctx):
    ctx.paso("Verificando requisitos", 0, 5)
    cidr = _requisitos(ctx)
    ctx.paso("Aprovisionando la instancia de monitoreo", 5, 40)
    motor = _motor("terraform", "monitoreo", config.TRABAJO / "plataforma" / "monitoreo", _backend_monitoreo(), ctx)
    motor.preparar({"region": config.REGION, "admin_cidr": cidr})
    motor.aplicar(ctx.progreso)
    salidas = motor.salidas()
    secretos = salidas.pop("secretos")
    ctx.redactor.agregar(_solo_secretos(secretos))
    db.set_config("monitoreo", {"grafana_url": salidas["grafana_url"], "prometheus_url": salidas["prometheus_url"],
                                "host": salidas["host"]})

    ctx.paso("Instalando Prometheus central, reglas de alerta y Grafana (Ansible)", 40, 97)
    inv = ansible.inventario_ini(
        {"monitoreo": [("asist-monitoreo", {"ansible_host": salidas["host"]["publica"]})]},
        {"ansible_user": "ubuntu", "ansible_python_interpreter": "/usr/bin/python3",
         "aws_region": config.REGION, "grafana_admin_password": secretos["grafana_admin"]})
    ansible.ejecutar_playbook("monitoreo.yml", inv, secretos["ssh_privada"], ctx.log, ctx.progreso, total_tareas=35)

    ctx.paso("Registrando versiones", 97, 100)
    _registrar_despliegue(ctx, "monitoreo", None, "desplegar_monitoreo", motor, "monitoreo")
    pendientes = [t["id"] for t in servicios.listar_tenants(estados=["activo"])
                  if not json.loads(t["salidas"] or "{}").get("monitoreo_conectado")]
    if pendientes:
        servicios.registrar_evento("plataforma", "Tenants sin conectar al monitoreo",
                                   "Vuelva a desplegarlos para abrir su Prometheus al monitoreo: " + ", ".join(pendientes),
                                   severidad="warning")
    return {"grafana_url": salidas["grafana_url"], "tenants_sin_conectar": pendientes}


def eliminar_monitoreo(ctx):
    ctx.paso("Verificando requisitos", 0, 5)
    cidr = _requisitos(ctx, necesita_docker=False)
    ctx.paso("Eliminando la instancia de monitoreo", 5, 100)
    motor = _motor("terraform", "monitoreo", config.TRABAJO / "plataforma" / "monitoreo", _backend_monitoreo(), ctx)
    motor.preparar({"region": config.REGION, "admin_cidr": cidr})
    motor.destruir(ctx.progreso)
    db.ejecutar("DELETE FROM config WHERE clave = 'monitoreo'")
    return {}


def preparar_estado(ctx):
    ctx.paso("Verificando requisitos", 0, 10)
    aws.identidad()
    ctx.paso("Creando el bucket S3 del estado (versionado, cifrado KMS, sin acceso público, solo TLS)", 10, 95)
    backend = {"tipo": "local", "path": (config.ESTADO_LOCAL / "plataforma" / "estado-bootstrap.tfstate").as_posix()}
    motor = _motor("terraform", "estado", config.TRABAJO / "plataforma" / "estado", backend, ctx)
    motor.preparar({"region": config.REGION})
    motor.aplicar(ctx.progreso)
    salidas = motor.salidas()
    db.set_config("estado_bucket", salidas["bucket"])
    db.set_config("estado_tipo", "s3")
    ctx.paso("Listo", 95, 100)
    ctx.log(f"Los tenants y el monitoreo NUEVOS guardarán su estado en s3://{salidas['bucket']}/")
    return {"bucket": salidas["bucket"]}


MANEJADORES = {
    "desplegar": desplegar,
    "suspender": suspender,
    "reanudar": reanudar,
    "eliminar": eliminar,
    "desplegar_monitoreo": desplegar_monitoreo,
    "eliminar_monitoreo": eliminar_monitoreo,
    "preparar_estado": preparar_estado,
}

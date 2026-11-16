"""Consultas síncronas que hace la interfaz (no modifican infraestructura)."""
import re

from motores import obtener as obtener_motor
from nucleo import config, db, servicios

from . import aws

DESCRIPCION = [
    (r"^aws_instance\.(\w+)$", "Instancia EC2 «{0}» y su disco raíz"),
    (r"^aws_eip\.(\w+)$", "IP elástica de {0} (la URL dejará de funcionar)"),
    (r"^aws_ec2_instance_state\.(\w+)$", "Control de encendido de {0}"),
    (r"^aws_security_group\.(\w+)$", "Grupo de seguridad de {0}"),
    (r"^aws_vpc_security_group_(ingress|egress)_rule\.(.+)$", "Regla de firewall ({0}) {1}"),
    (r"^aws_key_pair\.(\w+)$", "Par de claves SSH en AWS"),
    (r"^tls_private_key\.(\w+)$", "Clave privada SSH de administración"),
    (r"^random_password\.db$", "Contraseña de la base de datos"),
    (r"^random_password\.django_secret$", "Clave secreta de Django (firma de tokens)"),
    (r"^random_password\.admin_app$", "Contraseña del administrador de la app"),
]


def _describir(direccion):
    for patron, texto in DESCRIPCION:
        m = re.match(patron, direccion)
        if m:
            return texto.format(*m.groups())
    return direccion


def _motor(t, log=lambda _m: None):
    return obtener_motor(t["motor"], stack="tenant",
                         trabajo=config.TRABAJO / "tenants" / t["id"], backend=t["backend_estado"], log=log)


def _motor_listo(t):
    motor = _motor(t)
    if not motor.inicializado():
        plan = servicios.obtener_plan(t["plan_id"])
        from .trabajos import _variables_tenant
        motor.preparar(_variables_tenant(t, plan, "running", "0.0.0.0/32"))
    return motor


def vista_previa_eliminacion(tenant_id):
    t = servicios.obtener_tenant(tenant_id)
    plan = servicios.obtener_plan(t["plan_id"])
    direcciones = [d for d in _motor_listo(t).recursos() if not d.startswith("data.")]
    s = t["salidas"]
    datos = []
    if direcciones:
        datos = [
            f"Base de datos **asistencia_db** del tenant (módulos, estudiantes, fechas, asistencias y actividad) "
            f"en el volumen `{s.get('bd_volumen_id', '?')}` de {plan['disco_bd_gb']} GB.",
            "Código, configuración y registros de la aplicación en las instancias frontend y backend.",
            "Métricas históricas guardadas en el Prometheus del tenant.",
            "Contraseñas y claves del tenant (solo existen en su estado cifrado del motor de aprovisionamiento).",
        ]
    return {
        "recursos": [{"Recurso": _describir(d), "Dirección en el motor": d} for d in direcciones],
        "datos": datos,
        "conserva": [
            f"Snapshot final de la base de datos durante **{plan['retencion_dias']} días** (si lo marca abajo); "
            "después se borra automáticamente.",
            "Snapshots anteriores (por suspensión) hasta su fecha de vencimiento.",
            "El registro del tenant, su historial de operaciones y despliegues en la plataforma "
            "(el identificador queda reservado).",
        ],
    }


def credenciales_app(tenant_id):
    """Lee la contraseña del admin de la app desde el estado cifrado (no se guarda en la plataforma)."""
    t = servicios.obtener_tenant(tenant_id)
    sec = _motor_listo(t).salidas().get("secretos") or {}
    return {"usuario": "admin", "password": sec.get("admin_password")}


def credenciales_grafana():
    b = db.get_config("monitoreo_backend")
    if not b:
        return None
    motor = obtener_motor("terraform", stack="monitoreo",
                          trabajo=config.TRABAJO / "plataforma" / "monitoreo", backend=b, log=lambda _m: None)
    return {"usuario": "admin", "password": (motor.salidas().get("secretos") or {}).get("grafana_admin")}


def snapshots(tenant_id=None):
    return aws.listar_snapshots(tenant_id)

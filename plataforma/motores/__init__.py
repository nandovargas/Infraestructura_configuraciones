"""Registro de motores de aprovisionamiento.

Todos implementan la misma interfaz (motores.base.Motor):
  - terraform : plantillas HCL de infra/terraform/<stack>
  - opentofu  : las MISMAS plantillas HCL, con la CLI `tofu`
  - pulumi    : programa Python de infra/pulumi/<stack> (por ahora el stack "tenant")
Para habilitar uno nuevo basta con completar su clase y marcar disponible = True.
"""
from .base import Motor, MotorNoDisponible
from .pulumi import MotorPulumi
from .terraform import MotorOpenTofu, MotorTerraform

MOTORES = {
    "terraform": MotorTerraform,
    "opentofu": MotorOpenTofu,
    "pulumi": MotorPulumi,
    "cloudformation": type("MotorCloudFormation", (MotorNoDisponible,),
                           {"nombre": "cloudformation", "etiqueta": "AWS CloudFormation"}),
}


def para_tenants() -> dict:
    """Motores que se pueden elegir al crear un tenant."""
    return {k: c for k, c in MOTORES.items() if c.disponible and c.soporta("tenant")}


def obtener(nombre: str, stack: str | None = None, **kwargs) -> Motor:
    """Instancia un motor. Con `stack`, la plantilla se toma de la carpeta propia del motor."""
    clase = MOTORES.get(nombre)
    if clase is None:
        raise ValueError(f"Motor desconocido: {nombre}")
    if not clase.disponible:
        raise ValueError(f"El motor {clase.etiqueta} todavía no está disponible.")
    if stack is not None:
        if not clase.soporta(stack):
            raise ValueError(f"El motor {clase.etiqueta} no implementa el stack «{stack}».")
        kwargs["plantilla"] = clase.dir_plantillas / stack
    return clase(**kwargs)

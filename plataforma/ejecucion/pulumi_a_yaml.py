"""Convierte un programa Pulumi (Python) en un documento Pulumi YAML para analizarlo con KICS.

KICS solo entiende Pulumi en formato YAML. Este script EJECUTA el programa real con el motor
simulado de Pulumi (mocks): no llama a AWS ni necesita credenciales, y captura cada recurso
con las propiedades exactas que el programa enviaría al proveedor. El resultado es lo que
KICS analiza, así el reporte refleja el programa y no una copia mantenida a mano.

Se ejecuta con el Python del entorno de Pulumi (datos/cache/pulumi-venv):
    python pulumi_a_yaml.py <carpeta_programa> <salida.yaml> <config.json>
"""
import asyncio
import json
import re
import runpy
import sys
from pathlib import Path

import pulumi
import yaml

SECRETO = "4dabf18193072939515e22adb298388d"   # marca interna de Pulumi para valores secretos
RECURSOS: list[tuple[str, str, dict]] = []

# Atributos calculados por AWS que el programa lee de otros recursos
SALIDAS_SIMULADAS = {
    "aws:ec2/instance:Instance": {"privateIp": "10.0.0.10", "publicIp": "203.0.113.10",
                                  "rootBlockDevice": {"volumeId": "vol-simulado"}},
    "aws:ec2/eip:Eip": {"publicIp": "203.0.113.20"},
    "aws:ec2/keyPair:KeyPair": {"keyName": "clave-simulada"},
    "random:index/randomPassword:RandomPassword": {"result": "valor-simulado"},
    "tls:index/privateKey:PrivateKey": {"publicKeyOpenssh": "ssh-ed25519 AAAA-simulada",
                                        "privateKeyOpenssh": "clave-privada-simulada"},
}
CONSULTAS_SIMULADAS = {
    "aws:ec2/getVpc:getVpc": {"id": "vpc-simulada", "cidrBlock": "172.31.0.0/16"},
    "aws:ec2/getSubnets:getSubnets": {"ids": ["subnet-simulada"]},
    "aws:ec2/getSecurityGroups:getSecurityGroups": {"ids": ["sg-monitoreo-simulado"]},
    "aws:ec2/getAmi:getAmi": {"id": "ami-simulada"},
}


class Simulador(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        RECURSOS.append((args.typ, args.name, args.inputs))
        estado = {**args.inputs}
        for clave, valor in SALIDAS_SIMULADAS.get(args.typ, {}).items():
            if isinstance(valor, dict) and isinstance(estado.get(clave), dict):
                estado[clave] = {**estado[clave], **valor}
            else:
                estado.setdefault(clave, valor)
        return [f"{args.name}-id", estado]

    def call(self, args: pulumi.runtime.MockCallArgs):
        return CONSULTAS_SIMULADAS.get(args.token, {})


def limpiar(valor):
    """Quita las envolturas de secretos y los valores vacíos."""
    if isinstance(valor, dict):
        if valor.get(SECRETO) is not None and "value" in valor:
            return limpiar(valor["value"])
        return {k: limpiar(v) for k, v in valor.items() if v is not None}
    if isinstance(valor, (list, tuple)):
        return [limpiar(v) for v in valor]
    return valor


def tipo_yaml(tipo: str) -> str:
    """aws:ec2/instance:Instance -> aws:ec2:Instance (forma que usan Pulumi YAML y las reglas de KICS)."""
    return re.sub(r"^([^:]+:[^:/]+)/[^:]+:", r"\1:", tipo)


def convertir(carpeta: Path, configuracion: dict) -> dict:
    asyncio.set_event_loop(asyncio.new_event_loop())   # Python 3.12+ ya no crea uno implícito
    proyecto = yaml.safe_load((carpeta / "Pulumi.yaml").read_text(encoding="utf-8"))["name"]
    pulumi.runtime.set_mocks(Simulador(), project=proyecto, stack="analisis", preview=False)
    pulumi.runtime.set_all_config({f"{proyecto}:{k}": str(v) for k, v in configuracion.items()})

    @pulumi.runtime.test
    def ejecutar():
        runpy.run_path(str(carpeta / "__main__.py"), run_name="__main__")
        return pulumi.Output.from_input(True)

    ejecutar()
    recursos, usados = {}, set()
    for tipo, nombre, entradas in RECURSOS:
        if tipo.startswith("pulumi:"):
            continue   # Stack y proveedores: no son infraestructura
        corto = tipo_yaml(tipo)
        # En YAML el nombre lógico es único entre todos los tipos; en Python solo dentro de cada tipo
        clave = nombre if nombre not in usados else f"{nombre}-{corto.split(':')[-1]}"
        usados.add(clave)
        recursos[clave] = {"type": corto, "properties": limpiar(entradas)}
    return {"name": proyecto, "runtime": "yaml",
            "description": "Generado desde el programa Python para el analisis de seguridad (KICS). No editar.",
            "resources": recursos}


if __name__ == "__main__":
    carpeta, salida, config_json = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    documento = convertir(carpeta, json.loads(config_json.read_text(encoding="utf-8")))
    salida.write_text(yaml.safe_dump(documento, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    print(len(documento["resources"]))

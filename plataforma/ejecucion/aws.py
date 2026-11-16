"""Operaciones directas con AWS (boto3): identidad, IP pública y snapshots de retención.

Los snapshots se crean FUERA de Terraform para que sobrevivan a la eliminación del tenant;
llevan la etiqueta EliminarDespues y el worker los purga cuando vencen.
"""
from datetime import datetime, timedelta, timezone

import boto3
import requests

from nucleo import config
from nucleo.errores import ErrorOperacion


def _ec2():
    return boto3.client("ec2", region_name=config.REGION)


def identidad() -> dict:
    try:
        r = boto3.client("sts", region_name=config.REGION).get_caller_identity()
        return {"cuenta": r["Account"], "arn": r["Arn"]}
    except Exception as e:  # credenciales ausentes o vencidas
        raise ErrorOperacion("No se pudo validar la identidad en AWS", f"{type(e).__name__}: {e}")


def mi_cidr() -> str:
    """IP pública de la máquina de la plataforma (/32) para SSH y lectura de Prometheus."""
    import os
    if os.environ.get("PLATAFORMA_ADMIN_CIDR"):
        return os.environ["PLATAFORMA_ADMIN_CIDR"]
    try:
        ip = requests.get("https://checkip.amazonaws.com", timeout=10).text.strip()
        return f"{ip}/32"
    except Exception as e:
        raise ErrorOperacion("No se pudo detectar la IP pública de la plataforma", str(e))


def crear_snapshot(volumen_id: str, tenant_id: str, entorno: str, dias: int, motivo: str, log) -> str:
    expira = (datetime.now(timezone.utc) + timedelta(days=dias)).date().isoformat()
    ec2 = _ec2()
    snap = ec2.create_snapshot(
        VolumeId=volumen_id,
        Description=f"Asistencia SaaS - {tenant_id} ({entorno}) - {motivo}",
        TagSpecifications=[{"ResourceType": "snapshot", "Tags": [
            {"Key": "Plataforma", "Value": "asistencia-saas"},
            {"Key": "Tenant", "Value": tenant_id},
            {"Key": "Entorno", "Value": entorno},
            {"Key": "Motivo", "Value": motivo},
            {"Key": "EliminarDespues", "Value": expira},
            {"Key": "Name", "Value": f"asist-{tenant_id}-{entorno}-bd-{motivo}"},
        ]}])
    sid = snap["SnapshotId"]
    log(f"Snapshot {sid} del volumen de BD iniciado (se conserva hasta {expira}). Esperando a que termine…")
    try:
        ec2.get_waiter("snapshot_completed").wait(SnapshotIds=[sid], WaiterConfig={"Delay": 15, "MaxAttempts": 120})
    except Exception as e:
        raise ErrorOperacion("El snapshot de la base de datos no terminó a tiempo", f"{sid}: {e}")
    log(f"Snapshot {sid} completado.")
    return sid


def listar_snapshots(tenant_id: str | None = None) -> list[dict]:
    filtros = [{"Name": "tag:Plataforma", "Values": ["asistencia-saas"]}]
    if tenant_id:
        filtros.append({"Name": "tag:Tenant", "Values": [tenant_id]})
    res = []
    for s in _ec2().describe_snapshots(OwnerIds=["self"], Filters=filtros)["Snapshots"]:
        tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
        res.append({"snapshot": s["SnapshotId"], "tenant": tags.get("Tenant"), "entorno": tags.get("Entorno"),
                    "motivo": tags.get("Motivo"), "creado": s["StartTime"].isoformat(timespec="minutes"),
                    "expira": tags.get("EliminarDespues"), "gb": s["VolumeSize"], "estado": s["State"]})
    return sorted(res, key=lambda x: x["creado"], reverse=True)


def purgar_snapshots_vencidos(log) -> list[str]:
    hoy = datetime.now(timezone.utc).date().isoformat()
    borrados = []
    for s in listar_snapshots():
        if s["expira"] and s["expira"] < hoy and s["estado"] == "completed":
            _ec2().delete_snapshot(SnapshotId=s["snapshot"])
            borrados.append(s["snapshot"])
            log(f"Snapshot vencido eliminado: {s['snapshot']} ({s['tenant']}, venció {s['expira']})")
    return borrados

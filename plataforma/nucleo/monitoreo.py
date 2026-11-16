"""Consulta de métricas (API de Prometheus) y sincronización de alertas con la plataforma.

Fuente principal: el Prometheus central de la instancia de monitoreo (federa a todos los
tenants y evalúa las reglas de alerta). Si aún no existe, se puede consultar el
Prometheus propio del backend de un tenant (sin etiquetas de tenant ni alertas).
"""
import hashlib
import time
from datetime import datetime, timezone

import requests

from . import db

TIMEOUT = 6


def central():
    return db.get_config("monitoreo")


def _get(url, ruta, params):
    r = requests.get(url.rstrip("/") + ruta, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    datos = r.json()
    if datos.get("status") != "success":
        raise RuntimeError(datos.get("error", "consulta fallida"))
    return datos["data"]


def consulta(url, expr):
    return _get(url, "/api/v1/query", {"query": expr})["result"]


def rango(url, expr, horas=3, paso=60):
    fin = time.time()
    return _get(url, "/api/v1/query_range",
                {"query": expr, "start": fin - horas * 3600, "end": fin, "step": paso})["result"]


def filtro(tenant=None, entornos=None, central_=True):
    """Selector de etiquetas. En el Prometheus propio de un tenant no hay etiqueta tenant."""
    if not central_:
        return ""
    partes = []
    if tenant:
        partes.append(f'tenant="{tenant}"')
    if entornos:
        partes.append(f'entorno=~"{"|".join(entornos)}"')
    return ", " + ", ".join(partes) if partes else ""


def expresiones(f):
    lat = f"rate(django_http_requests_latency_seconds_by_view_method_bucket{{{f.lstrip(', ')}}}[5m])"
    resp = "django_http_responses_total_by_status_total"
    return {
        "disponibilidad": f'100 * avg by (tenant, entorno) (avg_over_time(up{{job="django"{f}}}[1h]))',
        "cpu": f'100 * (1 - avg by (tenant, entorno, rol) (rate(node_cpu_seconds_total{{mode="idle"{f}}}[5m])))',
        "memoria": f'100 * (1 - avg by (tenant, entorno, rol) (node_memory_MemAvailable_bytes{{rol!=""{f}}} '
                   f'/ node_memory_MemTotal_bytes{{rol!=""{f}}}))',
        "disco": f'100 * (1 - avg by (tenant, entorno, rol) (node_filesystem_avail_bytes{{mountpoint="/"{f}}} '
                 f'/ node_filesystem_size_bytes{{mountpoint="/"{f}}}))',
        "latencia_p95": f"histogram_quantile(0.95, sum by (tenant, entorno, le) ({lat}))",
        "peticiones": f'sum by (tenant, entorno) (rate(django_http_requests_total_by_method_total{{job="django"{f}}}[5m]))',
        "errores_pct": f'100 * sum by (tenant, entorno) (rate({resp}{{status=~"5.."{f}}}[5m])) '
                       f'/ clamp_min(sum by (tenant, entorno) (rate({resp}{{job="django"{f}}}[5m])), 0.001)',
        "bd_up": f'max by (tenant, entorno) (pg_up{{job="postgres"{f}}})',
        "bd_conexiones": f'sum by (tenant, entorno) (pg_stat_activity_count{{job="postgres"{f}}})',
        "servicios": f'up{{job=~"django|node|postgres"{f}}}',
    }


def instantaneas(url, f):
    """Valores actuales de todas las métricas clave. Devuelve {métrica: [(labels, valor)]}."""
    res = {}
    for nombre, expr in expresiones(f).items():
        try:
            res[nombre] = [(r["metric"], float(r["value"][1])) for r in consulta(url, expr)]
        except Exception as e:
            res[nombre] = e
    return res


# ---------- Alertas ----------
def _huella(alerta):
    etiquetas = {k: v for k, v in alerta["labels"].items() if k not in ("severity",)}
    clave = "|".join(f"{k}={etiquetas[k]}" for k in sorted(etiquetas))
    return hashlib.sha1(clave.encode()).hexdigest()


def sincronizar_alertas() -> tuple[int, int]:
    """Registra como eventos las alertas activas del Prometheus central y marca las resueltas."""
    m = central()
    if not m:
        return 0, 0
    alertas = [a for a in _get(m["prometheus_url"], "/api/v1/alerts", {})["alerts"] if a["state"] == "firing"]
    activas = set()
    nuevas = 0
    con = db.conectar()
    try:
        # Ventana de mantenimiento: al suspender/reanudar/desplegar/eliminar los servicios caen a
        # propósito; esas alertas no son incidentes. Si siguen al terminar, se registran.
        mantenimiento = {r["id"] for r in con.execute(
            "SELECT id FROM tenants WHERE estado IN ('aprovisionando','suspendiendo','suspendido','reanudando',"
            "'eliminando','eliminado')").fetchall()}
        alertas = [a for a in alertas if a["labels"].get("tenant") not in mantenimiento]
        for a in alertas:
            h = _huella(a)
            activas.add(h)
            lb = a["labels"]
            detectada = a.get("activeAt", db.ahora())
            cur = con.execute(
                "INSERT OR IGNORE INTO eventos(origen, tenant_id, entorno, severidad, titulo, detalle, huella, estado,"
                " detectado_en) VALUES('alerta', ?, ?, ?, ?, ?, ?, 'activa', ?)",
                (lb.get("tenant"), lb.get("entorno"), lb.get("severity", "warning"),
                 f"{lb.get('alertname')}: {a['annotations'].get('summary', '')}",
                 a["annotations"].get("description", ""), h, detectada[:19] + "+00:00"))
            nuevas += cur.rowcount
        resueltas = 0
        for fila in con.execute("SELECT id, huella FROM eventos WHERE estado = 'activa'").fetchall():
            if fila["huella"] not in activas:
                con.execute("UPDATE eventos SET estado = 'resuelta', resuelto_en = ? WHERE id = ?",
                            (datetime.now(timezone.utc).isoformat(timespec="seconds"), fila["id"]))
                resueltas += 1
    finally:
        con.close()
    return nuevas, resueltas

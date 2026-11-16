"""Análisis de seguridad del código de infraestructura con Checkmarx KICS.

KICS (Keeping Infrastructure as Code Secure) se ejecuta en su imagen Docker oficial. Analiza
el código, no la nube: no usa credenciales de AWS ni crea nada.

Fuentes analizadas (una por formato de código):
  - hcl    : infra/terraform/<stack>. Son los MISMOS archivos para Terraform y para OpenTofu,
             así que el reporte vale para los dos motores.
  - pulumi : infra/pulumi/<stack>. KICS solo entiende Pulumi en YAML, por eso el programa
             Python se ejecuta con el motor simulado de Pulumi (sin AWS) y sus recursos reales
             se vuelcan a un Pulumi.yaml, que es lo que KICS analiza (ver pulumi_a_yaml.py).

Cada análisis guarda en datos/escaneos/<id>/ los reportes (HTML, PDF, JSON, SARIF, CSV) y una
copia del código analizado, para poder consultar y descargar resultados anteriores.
"""
import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

from nucleo import config, db, versiones
from nucleo.errores import ErrorOperacion

from .ansible import _docker, docker_disponible

FUENTES = {
    "hcl": {"etiqueta": "Terraform / OpenTofu (HCL)", "tipo": "Terraform", "motores": ("terraform", "opentofu"),
            "plantillas": config.PLANTILLAS_TF},
    "pulumi": {"etiqueta": "Pulumi (Python → YAML)", "tipo": "Pulumi", "motores": ("pulumi",),
               "plantillas": config.PLANTILLAS_PULUMI},
}
SEVERIDADES = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"]
SEVERIDAD_ES = {"CRITICAL": "Crítica", "HIGH": "Alta", "MEDIUM": "Media", "LOW": "Baja", "INFO": "Informativa",
                "TRACE": "Traza"}
ICONO = {"CRITICAL": "🟣", "HIGH": "🔴", "MEDIUM": "🟠", "LOW": "🟡", "INFO": "🔵", "TRACE": "⚪"}
# formato -> (etiqueta, archivo, tipo MIME)
REPORTES = {
    "html": ("HTML", "resultados.html", "text/html"),
    "pdf": ("PDF", "resultados.pdf", "application/pdf"),
    "json": ("JSON", "resultados.json", "application/json"),
    "sarif": ("SARIF", "resultados.sarif", "application/sarif+json"),
    "csv": ("CSV", "resultados.csv", "text/csv"),
}
# Valores de ejemplo para ejecutar el programa Pulumi con el motor simulado
CONFIG_ANALISIS = {"region": "us-east-1", "tenant_id": "analisis", "entorno": "prod", "plan": "basico",
                   "tipo_frontend": "t3.micro", "tipo_backend": "t3.micro", "tipo_bd": "t3.micro",
                   "disco_app_gb": 16, "disco_bd_gb": 20, "estado": "running", "admin_cidr": "203.0.113.10/32",
                   "dominio_base": "nip.io"}
IGNORAR_HCL = shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfplan", "backend.tf",
                                     "terraform.tfvars.json", ".terraform.lock.hcl")


# ---------- qué se analiza ----------
def fuente_de(motor: str) -> str:
    """Fuente de código que usa un motor de aprovisionamiento."""
    return next((f for f, d in FUENTES.items() if motor in d["motores"]), "hcl")


def stacks(fuente: str) -> list[str]:
    carpeta = FUENTES[fuente]["plantillas"]
    encontrados = sorted(p.name for p in carpeta.iterdir() if p.is_dir()) if carpeta.is_dir() else []
    return sorted(encontrados, key=lambda s: s != "tenant")   # el stack del tenant primero


def _carpetas(fuente: str, stack: str) -> list[Path]:
    carpetas = [FUENTES[fuente]["plantillas"] / stack]
    compartido = config.PLANTILLAS_TF / stack / "cloud-init"
    if fuente == "pulumi" and compartido.is_dir():
        carpetas.append(compartido)   # el programa Pulumi incluye estas plantillas
    return carpetas


def huella_actual(fuente: str, stack: str) -> str:
    return versiones.huella(*_carpetas(fuente, stack))


# ---------- el escáner ----------
def imagen_instalada() -> bool:
    try:
        r = subprocess.run([_docker(), "image", "inspect", config.IMAGEN_KICS, "--format", "{{.Id}}"],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0
    except Exception:
        return False


def estado_escaner() -> dict:
    """Para la interfaz: si se puede analizar y, si no, por qué."""
    if not docker_disponible():
        return {"listo": False, "docker": False, "imagen": False,
                "mensaje": "Docker no está en ejecución. Abra Docker Desktop: KICS se ejecuta en un contenedor."}
    if not imagen_instalada():
        return {"listo": False, "docker": True, "imagen": False,
                "mensaje": f"La imagen `{config.IMAGEN_KICS}` no está descargada."}
    return {"listo": True, "docker": True, "imagen": True, "mensaje": f"KICS listo (`{config.IMAGEN_KICS}`)."}


def instalar() -> None:
    """Descarga la imagen de KICS (unos 100 MB la primera vez)."""
    if not docker_disponible():
        raise ErrorOperacion("Docker no está en ejecución (error during connect).")
    r = subprocess.run([_docker(), "pull", config.IMAGEN_KICS], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=1800)
    if r.returncode != 0:
        raise ErrorOperacion("No se pudo descargar la imagen de KICS", (r.stderr or r.stdout)[-2000:])


def _preparar_fuente(fuente: str, stack: str, destino: Path) -> None:
    """Deja en `destino` exactamente el código que KICS va a analizar."""
    origen = FUENTES[fuente]["plantillas"] / stack
    if not origen.is_dir():
        raise ErrorOperacion(f"No existe la plantilla {origen}")
    if fuente == "hcl":
        shutil.copytree(origen, destino, ignore=IGNORAR_HCL)
        return
    # Pulumi: ejecutar el programa con el motor simulado y volcarlo a YAML
    from motores.pulumi import IGNORAR, MotorPulumi
    programa = destino.parent / "programa"
    shutil.copytree(origen, programa, ignore=IGNORAR)
    compartido = config.PLANTILLAS_TF / stack / "cloud-init"
    if compartido.is_dir() and not (programa / "cloud-init").exists():
        shutil.copytree(compartido, programa / "cloud-init")
    MotorPulumi(origen, programa, {}, log=lambda _m: None)._asegurar_venv()
    python = config.PULUMI_VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    destino.mkdir(parents=True)
    parametros = destino.parent / "config.json"
    parametros.write_text(json.dumps(CONFIG_ANALISIS), encoding="utf-8")
    r = subprocess.run([str(python), str(Path(__file__).with_name("pulumi_a_yaml.py")), str(programa),
                        str(destino / "Pulumi.yaml"), str(parametros)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
                       env=_entorno_limpio())
    if r.returncode != 0:
        raise ErrorOperacion("No se pudo convertir el programa Pulumi a YAML para analizarlo",
                             (r.stderr or r.stdout)[-3000:])
    # El programa original acompaña al YAML (referencia y búsqueda de secretos en el código)
    shutil.copy2(origen / "__main__.py", destino / "__main__.py")


def _entorno_limpio() -> dict:
    """El programa se ejecuta simulado: no debe ver las credenciales de AWS ni la frase de Pulumi."""
    import os
    return {k: v for k, v in os.environ.items() if not k.startswith(("AWS_", "PULUMI_"))} | {"PYTHONIOENCODING": "utf-8"}


def escanear(fuente: str, stack: str = "tenant", usuario: str = "") -> int:
    """Analiza el código de una fuente y guarda los reportes. Devuelve el id del análisis."""
    if fuente not in FUENTES:
        raise ErrorOperacion(f"Fuente desconocida: {fuente}")
    estado = estado_escaner()
    if not estado["listo"]:
        raise ErrorOperacion(estado["mensaje"])
    inicio = time.monotonic()
    esc_id, _ = db.ejecutar(
        "INSERT INTO escaneos(fuente, stack, estado, huella, usuario, creado) VALUES(?,?, 'en_progreso', ?,?,?)",
        (fuente, stack, huella_actual(fuente, stack), usuario, db.ahora()))
    carpeta = config.ESCANEOS / str(esc_id)
    temporal = config.TMP / f"kics-{uuid.uuid4().hex[:10]}"
    try:
        shutil.rmtree(carpeta, ignore_errors=True)
        carpeta.mkdir(parents=True)
        _preparar_fuente(fuente, stack, temporal / "src")
        r = subprocess.run(
            [_docker(), "run", "--rm", "-v", f"{temporal / 'src'}:/src:ro", "-v", f"{carpeta}:/out",
             config.IMAGEN_KICS, "scan", "-p", "/src", "-o", "/out", "--output-name", "resultados",
             "--report-formats", ",".join(REPORTES), "-t", FUENTES[fuente]["tipo"],
             "--no-progress", "--no-color", "--minimal-ui", "--ignore-on-exit", "results"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        resultados = carpeta / "resultados.json"
        if r.returncode != 0 or not resultados.exists():
            raise ErrorOperacion("KICS no pudo completar el análisis", (r.stderr or r.stdout)[-3000:])
        shutil.copytree(temporal / "src", carpeta / "fuente")   # el código tal como se analizó
        datos = json.loads(resultados.read_text(encoding="utf-8"))
        db.ejecutar(
            "UPDATE escaneos SET estado = 'exitoso', version_kics = ?, archivos = ?, lineas = ?, reglas = ?,"
            " severidades = ?, total = ?, duracion_s = ? WHERE id = ?",
            (datos.get("kics_version", ""), datos.get("files_scanned", 0), datos.get("lines_scanned", 0),
             datos.get("queries_total", 0), json.dumps(datos.get("severity_counters", {})),
             datos.get("total_counter", 0), round(time.monotonic() - inicio, 1), esc_id))
    except Exception as e:
        detalle = f"{getattr(e, 'resumen', e)}\n{getattr(e, 'detalle', '')}".strip()
        db.ejecutar("UPDATE escaneos SET estado = 'fallido', error = ?, duracion_s = ? WHERE id = ?",
                    (detalle[-4000:], round(time.monotonic() - inicio, 1), esc_id))
        raise
    finally:
        shutil.rmtree(temporal, ignore_errors=True)
    _purgar(fuente, stack)
    return esc_id


def _purgar(fuente: str, stack: str) -> None:
    """Conserva solo los análisis más recientes de cada fuente y stack."""
    viejos = db.consultar("SELECT id FROM escaneos WHERE fuente = ? AND stack = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
                          (fuente, stack, config.ESCANEOS_CONSERVADOS))
    for v in viejos:
        shutil.rmtree(config.ESCANEOS / str(v["id"]), ignore_errors=True)
        db.ejecutar("DELETE FROM escaneos WHERE id = ?", (v["id"],))


# ---------- consulta de resultados ----------
def _fila(e: dict | None) -> dict | None:
    if e:
        e["severidades"] = json.loads(e["severidades"] or "{}")
    return e


def obtener(esc_id: int) -> dict | None:
    return _fila(db.uno("SELECT * FROM escaneos WHERE id = ?", (esc_id,)))


def ultimo(fuente: str, stack: str = "tenant") -> dict | None:
    """Último análisis terminado bien de una fuente."""
    return _fila(db.uno("SELECT * FROM escaneos WHERE fuente = ? AND stack = ? AND estado = 'exitoso' "
                        "ORDER BY id DESC LIMIT 1", (fuente, stack)))


def listar(fuente: str | None = None, limite: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM escaneos", []
    if fuente:
        sql, params = sql + " WHERE fuente = ?", [fuente]
    return [_fila(e) for e in db.consultar(sql + " ORDER BY id DESC LIMIT ?", (*params, limite))]


def desactualizado(esc: dict) -> bool:
    """El código cambió después de este análisis."""
    return esc["huella"] != huella_actual(esc["fuente"], esc["stack"])


def _archivo(nombre: str) -> str:
    """'../../src/main.tf' -> 'main.tf'."""
    partes = nombre.replace("\\", "/").split("/src/", 1)
    return partes[1] if len(partes) == 2 else nombre.lstrip("./")


def hallazgos(esc_id: int) -> list[dict]:
    """Hallazgos del análisis, uno por línea afectada, ordenados de mayor a menor severidad."""
    ruta = config.ESCANEOS / str(esc_id) / "resultados.json"
    if not ruta.exists():
        return []
    return aplanar(json.loads(ruta.read_text(encoding="utf-8")))


def aplanar(datos: dict) -> list[dict]:
    filas = []
    for q in datos.get("queries") or []:
        for f in q.get("files") or []:
            filas.append({
                "severidad": q.get("severity", "INFO"), "regla": q.get("query_name", ""),
                "categoria": q.get("category", ""), "archivo": _archivo(f.get("file_name", "")),
                "linea": f.get("line", 0), "recurso_tipo": f.get("resource_type", ""),
                "recurso": f.get("resource_name", ""), "esperado": f.get("expected_value", ""),
                "actual": f.get("actual_value", ""), "descripcion": q.get("description", ""),
                "url": q.get("query_url", ""), "cwe": q.get("cwe", ""), "regla_id": q.get("query_id", ""),
                "plataforma": q.get("platform", ""),
            })
    orden = {s: i for i, s in enumerate(SEVERIDADES)}
    return sorted(filas, key=lambda h: (orden.get(h["severidad"], 99), h["regla"], h["archivo"], h["linea"]))


def reporte(esc_id: int, formato: str) -> Path | None:
    """Ruta del reporte en el formato pedido (html, pdf, json, sarif, csv) si existe."""
    if formato not in REPORTES:
        return None
    ruta = config.ESCANEOS / str(esc_id) / REPORTES[formato][1]
    return ruta if ruta.exists() else None


def fragmento(esc_id: int, archivo: str, linea: int, contexto: int = 3) -> str:
    """Líneas del código analizado alrededor de un hallazgo (con la línea marcada)."""
    base = (config.ESCANEOS / str(esc_id) / "fuente").resolve()
    ruta = (base / archivo).resolve()
    if base not in ruta.parents or not ruta.is_file() or linea < 1:
        return ""
    lineas = ruta.read_text(encoding="utf-8", errors="replace").splitlines()
    desde, hasta = max(linea - contexto, 1), min(linea + contexto, len(lineas))
    return "\n".join(f"{'>' if n == linea else ' '} {n:4d} | {lineas[n - 1][:160]}" for n in range(desde, hasta + 1))


def version() -> str:
    r = subprocess.run([_docker(), "run", "--rm", config.IMAGEN_KICS, "version"], capture_output=True, text=True,
                       timeout=120)
    return r.stdout.strip().split()[-1] if r.returncode == 0 and r.stdout.strip() else ""

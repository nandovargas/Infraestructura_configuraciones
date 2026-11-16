"""Versiones de la aplicación, de las plantillas y de las herramientas usadas en un despliegue."""
import hashlib
import platform
import shutil
import subprocess
from pathlib import Path

from . import config

EXCLUIR = {".git", ".terraform", "node_modules", "venv", ".venv", "__pycache__", "dist", "staticfiles", "datos"}
EXT_EXCLUIDAS = {".pyc", ".log", ".err", ".tfstate", ".xlsx"}


def huella(*carpetas: Path) -> str:
    """sha256 del contenido (rutas + bytes) de las carpetas: cambia si cambia cualquier archivo."""
    h = hashlib.sha256()
    for carpeta in carpetas:
        for f in sorted(p for p in carpeta.rglob("*") if p.is_file()):
            rel = f.relative_to(carpeta)
            if EXCLUIR & set(rel.parts) or f.suffix in EXT_EXCLUIDAS or f.name.startswith(".env"):
                continue
            h.update(rel.as_posix().encode())
            h.update(f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:12]


def _git(carpeta: Path) -> str:
    if not (carpeta / ".git").exists() or not shutil.which("git"):
        return ""
    try:
        commit = subprocess.run(["git", "-C", str(carpeta), "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True, timeout=10).stdout.strip()
        sucio = subprocess.run(["git", "-C", str(carpeta), "status", "--porcelain"], capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return commit + ("-modificado" if sucio else "")
    except Exception:
        return ""


def version_app(carpeta: Path) -> str:
    """git <commit>[-modificado] + huella del contenido realmente desplegado."""
    g = _git(carpeta)
    return f"{g} · {huella(carpeta)}" if g else huella(carpeta)


def version_plantillas(stack: str, carpeta: Path | None = None) -> str:
    """Huella de la plantilla del stack (la del motor usado) + roles de Ansible."""
    return f"tpl-{huella(carpeta or config.PLANTILLAS_TF / stack, config.ANSIBLE_DIR)}"


def herramientas_base() -> dict:
    res = {"plataforma": config.VERSION_PLATAFORMA, "python": platform.python_version()}
    try:
        import streamlit
        res["streamlit"] = streamlit.__version__
    except Exception:
        pass
    try:
        import boto3
        res["boto3"] = boto3.__version__
    except Exception:
        pass
    if shutil.which("docker"):
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True)
        res["docker"] = r.stdout.strip() or "no disponible"
    return res

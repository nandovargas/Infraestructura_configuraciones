"""Protección de secretos en registros y pantallas, y login del administrador."""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading

from . import config

MASCARA = "••••••"

# Patrones que nunca deben mostrarse aunque no se conozca el valor exacto
_PATRONES = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[clave privada oculta]"),
    (re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"), "[id de clave AWS oculto]"),
    (re.compile(r"(?i)((?:db_)?password|passwd|secret(?:_key)?|token|clave|grafana_admin(?:_password)?)"
                r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',}]+)"), r"\1\2" + MASCARA),
    (re.compile(r"(?i)(postgres(?:ql)?://[^:/\s]+:)([^@\s]+)(@)"), r"\1" + MASCARA + r"\3"),
    (re.compile(r"(?i)(Authorization:\s*Bearer\s+)\S+"), r"\1" + MASCARA),
]


class Redactor:
    """Reemplaza valores secretos conocidos y patrones típicos por una máscara."""

    def __init__(self):
        self._valores: set[str] = set()
        self._lock = threading.Lock()
        for var in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID"):
            if os.environ.get(var):
                self.agregar(os.environ[var])

    def agregar(self, valor):
        if isinstance(valor, dict):
            for v in valor.values():
                self.agregar(v)
            return
        if isinstance(valor, str):
            # la clave SSH se oculta por líneas (aparece partida en algunos mensajes)
            lineas = [l for l in valor.splitlines() if len(l) >= 16 and not l.startswith("-----")]
            partes = [valor] + (lineas if "\n" in valor else [])
            with self._lock:
                self._valores.update(p for p in partes if len(p) >= 6)

    def limpiar(self, texto: str) -> str:
        if not texto:
            return texto
        # Primero los patrones (bloques completos como claves privadas), luego los valores conocidos
        for patron, reemplazo in _PATRONES:
            texto = patron.sub(reemplazo, texto)
        with self._lock:
            valores = sorted(self._valores, key=len, reverse=True)
        for v in valores:
            if v in texto:
                texto = texto.replace(v, MASCARA)
        return texto


def limpiar(texto: str) -> str:
    """Limpieza por patrones (cuando no se conocen los valores exactos)."""
    return Redactor().limpiar(texto)


# ---------- Administrador de la plataforma ----------
def _hash(clave: str, sal: bytes) -> str:
    return hashlib.scrypt(clave.encode(), salt=sal, n=2 ** 14, r=8, p=1, dklen=32).hex()


def admin_configurado() -> bool:
    return config.ADMIN_FILE.exists()


def crear_admin(usuario: str, clave: str):
    sal = secrets.token_bytes(16)
    datos = {"usuario": usuario, "sal": sal.hex(), "hash": _hash(clave, sal)}
    config.ADMIN_FILE.write_text(json.dumps(datos), encoding="utf-8")
    try:
        os.chmod(config.ADMIN_FILE, 0o600)
    except OSError:
        pass


def verificar_admin(usuario: str, clave: str) -> bool:
    if not admin_configurado():
        return False
    datos = json.loads(config.ADMIN_FILE.read_text(encoding="utf-8"))
    esperado = _hash(clave, bytes.fromhex(datos["sal"]))
    return hmac.compare_digest(usuario, datos["usuario"]) and hmac.compare_digest(esperado, datos["hash"])

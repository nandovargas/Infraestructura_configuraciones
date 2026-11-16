"""Rutas y ajustes de la plataforma. Nada secreto vive aquí: los secretos se leen
del entorno (credenciales AWS) o del estado cifrado de Terraform."""
import os
from pathlib import Path

VERSION_PLATAFORMA = "1.0.0"

PLATAFORMA_DIR = Path(__file__).resolve().parent.parent
RAIZ = PLATAFORMA_DIR.parent                      # repositorio completo
DATOS = Path(os.environ.get("PLATAFORMA_DATOS", PLATAFORMA_DIR / "datos")).resolve()

DB_PATH = DATOS / "plataforma.db"
LOGS = DATOS / "logs"
TRABAJO = DATOS / "trabajo"            # copias de trabajo de Terraform por tenant
ESTADO_LOCAL = DATOS / "estado"        # estado de Terraform en modo local
TMP = DATOS / "tmp"                    # inventarios y claves temporales (se borran al terminar)
CACHE_PLUGINS = DATOS / "cache" / "plugins"
ESCANEOS = DATOS / "escaneos"         # reportes de seguridad de KICS (uno por análisis)
ADMIN_FILE = DATOS / "admin.json"      # hash scrypt de la contraseña del administrador

PLANTILLAS_TF = RAIZ / "infra" / "terraform"        # HCL: Terraform y OpenTofu
PLANTILLAS_PULUMI = RAIZ / "infra" / "pulumi"       # programas Pulumi (Python)
PULUMI_VENV = DATOS / "cache" / "pulumi-venv"       # entorno Python compartido por los stacks Pulumi
# Frase de cifrado de los secretos en el estado de Pulumi. Si no viene en el entorno
# (PULUMI_CONFIG_PASSPHRASE) se genera una vez y se guarda aquí. ¡Respáldela: sin ella
# no se pueden leer los secretos de los tenants creados con Pulumi!
PULUMI_FRASE = DATOS / "pulumi.frase"
ANSIBLE_DIR = RAIZ / "ansible"
APP_BACKEND = RAIZ / "asistencia_backend"
APP_FRONTEND = RAIZ / "asistencia_frontend"

REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
# Rutas explícitas de los binarios (opcional; si no, se buscan en el PATH y en las rutas de instalación habituales)
BINARIOS = {"terraform": os.environ.get("PLATAFORMA_TERRAFORM"), "tofu": os.environ.get("PLATAFORMA_TOFU"),
            "pulumi": os.environ.get("PLATAFORMA_PULUMI")}
IMAGEN_ANSIBLE = "asistencia-ansible:local"
# Escáner de seguridad de IaC (Checkmarx KICS). Se distribuye como imagen Docker.
IMAGEN_KICS = os.environ.get("PLATAFORMA_KICS_IMAGEN", "checkmarx/kics:v2.2.1")
ESCANEOS_CONSERVADOS = 15              # análisis que se guardan por cada fuente y stack
MAX_TRABAJOS_PARALELOS = int(os.environ.get("PLATAFORMA_TRABAJOS_PARALELOS", "3"))
ENTORNOS = ["dev", "staging", "prod"]
# URL de cada tenant: http://<identificador>.<IP del frontend>[.<dominio>]/panel/login
# "nip.io" es un DNS comodín público: upsa.54.172.11.111.nip.io resuelve a 54.172.11.111.
# Vacío = sin sufijo (http://upsa.54.172.11.111), solo resuelve con un DNS/hosts propio.
DOMINIO_TENANTS = os.environ.get("PLATAFORMA_DOMINIO", "nip.io").strip().strip(".")


def url_tenant(tenant_id: str, ip: str) -> str:
    return f"http://{tenant_id}.{ip}" + (f".{DOMINIO_TENANTS}" if DOMINIO_TENANTS else "")

for d in (DATOS, LOGS, TRABAJO, ESTADO_LOCAL, TMP, CACHE_PLUGINS, ESCANEOS):
    d.mkdir(parents=True, exist_ok=True)

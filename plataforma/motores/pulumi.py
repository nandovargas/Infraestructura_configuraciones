"""Motor Pulumi (programa en Python, backend de estado "DIY": carpeta local o S3).

- Programa: infra/pulumi/<stack> (Pulumi.yaml + __main__.py + requirements.txt). Reutiliza las
  plantillas cloud-init de infra/terraform/<stack>/cloud-init, que se copian a la copia de trabajo.
- Entorno Python: uno compartido (datos/cache/pulumi-venv); se crea o actualiza solo cuando
  cambia requirements.txt.
- Estado: mismo lugar que los demás motores (estado local de la plataforma o el bucket S3),
  en un prefijo propio de Pulumi. Un stack por tenant.
- Secretos: cifrados en el estado con una frase (PULUMI_CONFIG_PASSPHRASE). Si no viene en el
  entorno, se genera una vez en datos/pulumi.frase.
"""
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

from nucleo import config
from nucleo.errores import ErrorOperacion

from .base import Motor, Progreso

PROYECTO = "asistencia-tenant"
IGNORAR = shutil.ignore_patterns("venv", "__pycache__", "*.pyc", "Pulumi.*.yaml")

# Tipo de Pulumi -> tipo de Terraform: las direcciones se muestran igual con cualquier motor
TIPOS_TF = {
    "aws:ec2/instance:Instance": "aws_instance",
    "aws:ec2/eip:Eip": "aws_eip",
    "aws:ec2transitgateway/instanceState:InstanceState": "aws_ec2_instance_state",
    "aws:ec2/securityGroup:SecurityGroup": "aws_security_group",
    "aws:vpc/securityGroupIngressRule:SecurityGroupIngressRule": "aws_vpc_security_group_ingress_rule",
    "aws:vpc/securityGroupEgressRule:SecurityGroupEgressRule": "aws_vpc_security_group_egress_rule",
    "aws:ec2/keyPair:KeyPair": "aws_key_pair",
    "tls:index/privateKey:PrivateKey": "tls_private_key",
    "random:index/randomPassword:RandomPassword": "random_password",
}
# "    + 31 created" / "    2 unchanged" / "    +-1 replaced" en el resumen final
RE_RESUMEN = re.compile(r"^\s*(?:[+\-~]+\s*)?(\d+)\s+(created|updated|deleted|replaced|unchanged)\s*$")
# " +  aws:ec2:Instance frontend created (35s)"
RE_HECHO = re.compile(r"^\s*[+\-~]{1,2}\s+(\S+)\s+(\S+)\s+(created|updated|deleted|replaced)\b")
CLAVES_RESUMEN = {"created": "add", "updated": "change", "deleted": "remove", "replaced": "replace",
                  "unchanged": "unchanged"}
RECURSOS_TENANT = 32  # recursos aproximados de un stack nuevo (para la barra de progreso)


class MotorPulumi(Motor):
    nombre = "pulumi"
    etiqueta = "Pulumi"
    binario = "pulumi"
    candidatos = ("%ProgramFiles(x86)%/Pulumi/pulumi.exe", "%ProgramFiles%/Pulumi/pulumi.exe",
                  "~/.pulumi/bin/pulumi.exe", "~/.pulumi/bin/pulumi", "C:/ProgramData/chocolatey/bin/pulumi.exe",
                  "/usr/local/bin/pulumi", "/opt/homebrew/bin/pulumi")
    dir_plantillas = config.PLANTILLAS_PULUMI
    stacks = {"tenant"}

    # ---------- backend de estado y stack ----------
    @property
    def stack(self) -> str:
        """Nombre del stack: el identificador del tenant (último tramo de la ruta del estado)."""
        b = self.backend
        return Path(b["key"]).parent.name if b["tipo"] == "s3" else Path(b["path"]).stem

    def url_backend(self) -> str:
        b = self.backend
        if b["tipo"] == "s3":
            prefijo = f"pulumi/{Path(b['key']).parent.as_posix()}"
            return f"s3://{b['bucket']}/{prefijo}?region={b['region']}&awssdk=v2"
        carpeta = Path(b["path"]).with_suffix("").parent / f"{self.stack}.pulumi"
        carpeta.mkdir(parents=True, exist_ok=True)
        return "file://" + carpeta.resolve().as_posix()

    def _backend_desc(self):
        b = self.backend
        return self.url_backend().split("?")[0] + (" (cifrado, con bloqueo)" if b["tipo"] == "s3" else "")

    @staticmethod
    def frase() -> str:
        import os
        if os.environ.get("PULUMI_CONFIG_PASSPHRASE"):
            return os.environ["PULUMI_CONFIG_PASSPHRASE"]
        if not config.PULUMI_FRASE.exists():
            config.PULUMI_FRASE.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            try:
                os.chmod(config.PULUMI_FRASE, 0o600)
            except OSError:
                pass
        return config.PULUMI_FRASE.read_text(encoding="utf-8").strip()

    # ---------- utilidades ----------
    def _bin(self):
        ruta = self.ruta_binario()
        if not ruta:
            raise ErrorOperacion("No se encontró 'pulumi' en el PATH.")
        return ruta

    def _env(self):
        import os
        env = os.environ.copy()
        env.update({"PULUMI_BACKEND_URL": self.url_backend(), "PULUMI_CONFIG_PASSPHRASE": self.frase(),
                    "PULUMI_SKIP_UPDATE_CHECK": "true", "PULUMI_SKIP_CONFIRMATIONS": "true",
                    "PULUMI_DIY_BACKEND_NO_LEGACY_WARNING": "true", "NO_COLOR": "1",
                    "PYTHONIOENCODING": "utf-8"})
        env.pop("PULUMI_CONFIG_PASSPHRASE_FILE", None)
        env.update(self.entorno_proc)
        return env

    def _correr(self, args, al_linea=None, capturar=False):
        cmd = [self._bin(), *args, "--non-interactive", "--color", "never"]
        if capturar:
            r = subprocess.run(cmd, cwd=self.trabajo, env=self._env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise ErrorOperacion(f"Pulumi {args[0]} falló", r.stderr[-4000:] or r.stdout[-4000:])
            return r.stdout
        proc = subprocess.Popen(cmd, cwd=self.trabajo, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        errores = []
        for linea in proc.stdout:
            err = (al_linea or self._linea_simple)(linea.rstrip("\n"))
            if err:
                errores.append(err)
        proc.wait()
        if proc.returncode != 0:
            raise ErrorOperacion(f"Pulumi {args[0]} falló", "\n".join(errores[-15:]))

    def _linea_simple(self, linea):
        self.log(linea)
        return linea.strip() if re.search(r"(?i)\berror\b", linea) else None

    def _linea_progreso(self, progreso: Progreso | None, estado: dict):
        """Registra la salida, cuenta los recursos terminados y lee el resumen final."""
        def manejar(linea):
            if not linea.strip() or linea.lstrip().startswith("@"):
                return None   # "@ updating...." cada segundo: ruido
            self.log(linea)
            m = RE_HECHO.match(linea)
            if m and m.group(1) != "pulumi:pulumi:Stack":
                estado["hechos"] += 1
                if progreso:
                    progreso(min(estado["hechos"] / max(estado["total"], estado["hechos"], 1), 0.99), linea.strip())
            r = RE_RESUMEN.match(linea)
            if r:
                estado["resumen"][CLAVES_RESUMEN[r.group(2)]] = int(r.group(1))
            return linea.strip() if re.search(r"(?i)\berror\b", linea) else None
        return manejar

    def _asegurar_venv(self):
        """Crea o actualiza el entorno Python del programa (solo si cambió requirements.txt)."""
        requisitos = self.plantilla / "requirements.txt"
        huella = hashlib.sha256(requisitos.read_bytes()).hexdigest()[:16]
        marca = config.PULUMI_VENV / ".requisitos"
        python = config.PULUMI_VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        if python.exists() and marca.exists() and marca.read_text().strip() == huella:
            return
        if not python.exists():
            self.log(f"Creando el entorno Python de Pulumi en {config.PULUMI_VENV}")
            subprocess.run([sys.executable, "-m", "venv", str(config.PULUMI_VENV)], check=True)
        self.log("Instalando los paquetes del programa Pulumi (pulumi, pulumi-aws, pulumi-random, pulumi-tls)")
        r = subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(requisitos)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise ErrorOperacion("No se pudieron instalar los paquetes de Python de Pulumi", r.stderr[-4000:])
        marca.write_text(huella)

    # ---------- interfaz ----------
    def inicializado(self) -> bool:
        return (self.trabajo / "Pulumi.yaml").exists()

    def preparar(self, variables: dict) -> None:
        self._asegurar_venv()
        self.trabajo.mkdir(parents=True, exist_ok=True)
        # Copia limpia del programa. Se conserva Pulumi.<stack>.yaml (sal del cifrado de secretos).
        for item in self.trabajo.iterdir():
            if not (item.name.startswith("Pulumi.") and item.name != "Pulumi.yaml"):
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(self.plantilla, self.trabajo, ignore=IGNORAR, dirs_exist_ok=True)
        compartido = config.PLANTILLAS_TF / self.plantilla.name / "cloud-init"
        if compartido.is_dir() and not (self.trabajo / "cloud-init").exists():
            shutil.copytree(compartido, self.trabajo / "cloud-init")
        proyecto = (self.trabajo / "Pulumi.yaml").read_text(encoding="utf-8")
        proyecto = re.sub(r"(?m)^(\s*virtualenv:).*$", lambda m: f"{m.group(1)} {config.PULUMI_VENV.as_posix()}",
                          proyecto)
        (self.trabajo / "Pulumi.yaml").write_text(proyecto, encoding="utf-8")

        self.log(f"$ pulumi stack select {self.stack} --create  (estado: {self._backend_desc()})")
        self._correr(["stack", "select", self.stack, "--create"], capturar=True)
        valores = {**variables, "gestion": "pulumi"}
        args = ["config", "set-all", "--stack", self.stack]
        for k, v in valores.items():
            args += ["--plaintext", f"{k}={'' if v is None else v}"]
        self._correr(args, capturar=True)

    def aplicar(self, progreso: Progreso) -> dict:
        self.log("$ pulumi up")
        total = len(self.recursos()) or RECURSOS_TENANT
        estado = {"total": total, "hechos": 0, "resumen": {}}
        self._correr(["up", "--yes", "--skip-preview", "--suppress-outputs", "--stack", self.stack],
                     self._linea_progreso(progreso, estado))
        return estado["resumen"]

    def refrescar(self) -> None:
        # "up --refresh" (y no solo "refresh") para que las SALIDAS también se recalculen
        self.log("$ pulumi up --refresh")
        self._correr(["up", "--yes", "--skip-preview", "--refresh", "--suppress-outputs", "--stack", self.stack],
                     self._linea_progreso(None, {"total": 0, "hechos": 0, "resumen": {}}))

    def destruir(self, progreso: Progreso) -> dict:
        self.log("$ pulumi destroy")
        estado = {"total": len(self.recursos()) or RECURSOS_TENANT, "hechos": 0, "resumen": {}}
        self._correr(["destroy", "--yes", "--skip-preview", "--stack", self.stack],
                     self._linea_progreso(progreso, estado))
        return estado["resumen"]

    def recursos(self) -> list[str]:
        datos = json.loads(self._correr(["stack", "export", "--stack", self.stack], capturar=True) or "{}")
        return direcciones(datos)

    def salidas(self) -> dict:
        return json.loads(self._correr(["stack", "output", "--json", "--show-secrets", "--stack", self.stack],
                                       capturar=True) or "{}")

    def versiones(self) -> dict:
        res = {self.nombre: self._correr(["version"], capturar=True).strip().lstrip("v")}
        python = config.PULUMI_VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        r = subprocess.run([str(python), "-c", "import json; from importlib.metadata import version as v; "
                            "print(json.dumps({p: v(p) for p in ('pulumi-aws', 'pulumi-random', 'pulumi-tls')}))"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            res |= {k.removeprefix("pulumi-"): v for k, v in json.loads(r.stdout).items()}
        return res


def direcciones(exportado: dict) -> list[str]:
    """Recursos de `pulumi stack export` como direcciones estilo Terraform (aws_instance.frontend)."""
    res = []
    for r in (exportado.get("deployment") or {}).get("resources") or []:
        tipo = r.get("type", "")
        if tipo == "pulumi:pulumi:Stack" or tipo.startswith("pulumi:providers:") or r.get("delete"):
            continue
        nombre = r.get("urn", "").split("::")[-1]
        res.append(f"{TIPOS_TF.get(tipo, tipo)}.{nombre}")
    return res

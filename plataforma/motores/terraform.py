"""Motor Terraform (y OpenTofu, que comparte formato y CLI)."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

from nucleo import config
from nucleo.errores import ErrorOperacion

from .base import Motor, Progreso

IGNORAR = shutil.ignore_patterns(".terraform", "*.tfstate", "*.tfstate.*", "*.tfplan", "backend.tf",
                                 "terraform.tfvars.json")


class MotorTerraform(Motor):
    nombre = "terraform"
    etiqueta = "Terraform"
    binario = "terraform"
    candidatos = ("C:/terraform/terraform.exe", "%ProgramFiles%/Terraform/terraform.exe",
                  "%LOCALAPPDATA%/Microsoft/WinGet/Packages/Hashicorp.Terraform_*/terraform.exe",
                  "/usr/local/bin/terraform", "/opt/homebrew/bin/terraform")

    # ---------- utilidades ----------
    def _bin(self):
        ruta = self.ruta_binario()
        if not ruta:
            raise ErrorOperacion(f"No se encontró '{self.binario}' en el PATH.")
        return ruta

    def inicializado(self) -> bool:
        return (self.trabajo / ".terraform").exists()

    def _env(self):
        env = os.environ.copy()
        env.update({"TF_IN_AUTOMATION": "1", "TF_INPUT": "0", "CHECKPOINT_DISABLE": "1",
                    "TF_PLUGIN_CACHE_DIR": str(config.CACHE_PLUGINS), "NO_COLOR": "1"})
        env.update(self.entorno_proc)
        return env

    def _correr(self, args, al_linea=None, capturar=False):
        """Ejecuta el binario. Con al_linea, transmite la salida línea a línea."""
        cmd = [self._bin(), *args]
        if capturar:
            r = subprocess.run(cmd, cwd=self.trabajo, env=self._env(), capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                raise ErrorOperacion(f"{self.etiqueta} {args[0]} falló", r.stderr[-4000:] or r.stdout[-4000:])
            return r.stdout
        proc = subprocess.Popen(cmd, cwd=self.trabajo, env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
        errores = []
        for linea in proc.stdout:
            linea = linea.rstrip("\n")
            err = (al_linea or self._linea_simple)(linea)
            if err:
                errores.append(err)
        proc.wait()
        if proc.returncode != 0:
            raise ErrorOperacion(f"{self.etiqueta} {args[0]} falló", "\n\n".join(errores[-5:]))

    def _linea_simple(self, linea):
        self.log(linea)
        return linea if re.search(r"(?i)error", linea) else None

    def _linea_json(self, progreso: Progreso | None, estado: dict):
        """Interpreta la salida -json: registra el mensaje y calcula el avance."""
        def manejar(linea):
            try:
                ev = json.loads(linea)
            except json.JSONDecodeError:
                self.log(linea)
                return None
            tipo = ev.get("type")
            msg = ev.get("@message", "")
            if tipo == "diagnostic":
                d = ev.get("diagnostic", {})
                texto = f"{d.get('severity', '').upper()}: {d.get('summary', '')}\n{d.get('detail', '')}".strip()
                self.log(texto)
                return texto if d.get("severity") == "error" else None
            if tipo in ("apply_progress",):
                return None  # "Still creating..." cada 10 s: ruido
            if msg:
                self.log(msg)
            if tipo == "planned_change":
                estado["total"] += 1
            elif tipo in ("apply_complete", "apply_errored"):
                estado["hechos"] += 1
                if progreso and estado["total"]:
                    progreso(min(estado["hechos"] / estado["total"], 1.0), msg)
            elif tipo == "change_summary":
                estado["resumen"] = ev.get("changes", {})
            return None
        return manejar

    # ---------- interfaz ----------
    def preparar(self, variables: dict) -> None:
        self.trabajo.mkdir(parents=True, exist_ok=True)
        # Copia limpia de la plantilla (se conserva .terraform para no descargar proveedores otra vez)
        for item in self.trabajo.iterdir():
            if item.name not in (".terraform",):
                shutil.rmtree(item) if item.is_dir() else item.unlink()
        shutil.copytree(self.plantilla, self.trabajo, ignore=IGNORAR, dirs_exist_ok=True)
        (self.trabajo / "backend.tf").write_text(self._backend_hcl(), encoding="utf-8")
        (self.trabajo / "terraform.tfvars.json").write_text(json.dumps(variables, indent=2), encoding="utf-8")
        self.log(f"$ {self.binario} init  (estado: {self._backend_desc()})")
        self._correr(["init", "-input=false", "-reconfigure", "-no-color"])

    def _backend_hcl(self):
        b = self.backend
        if b["tipo"] == "s3":
            return ('terraform {\n  backend "s3" {\n'
                    f'    bucket       = "{b["bucket"]}"\n    key          = "{b["key"]}"\n'
                    f'    region       = "{b["region"]}"\n    encrypt      = true\n'
                    '    use_lockfile = true\n  }\n}\n')
        Path(b["path"]).parent.mkdir(parents=True, exist_ok=True)
        return f'terraform {{\n  backend "local" {{\n    path = "{b["path"]}"\n  }}\n}}\n'

    def _backend_desc(self):
        b = self.backend
        return f"s3://{b['bucket']}/{b['key']} (cifrado, con bloqueo)" if b["tipo"] == "s3" else f"local {b['path']}"

    def aplicar(self, progreso: Progreso) -> dict:
        self.log(f"$ {self.binario} apply")
        estado = {"total": 0, "hechos": 0, "resumen": {}}
        self._correr(["apply", "-auto-approve", "-input=false", "-json"], self._linea_json(progreso, estado))
        return estado["resumen"]

    def refrescar(self) -> None:
        self.log(f"$ {self.binario} apply -refresh-only")
        self._correr(["apply", "-refresh-only", "-auto-approve", "-input=false", "-json"],
                     self._linea_json(None, {"total": 0, "hechos": 0}))

    def destruir(self, progreso: Progreso) -> dict:
        self.log(f"$ {self.binario} destroy")
        estado = {"total": 0, "hechos": 0, "resumen": {}}
        self._correr(["destroy", "-auto-approve", "-input=false", "-json"], self._linea_json(progreso, estado))
        return estado["resumen"]

    def recursos(self) -> list[str]:
        try:
            salida = self._correr(["state", "list"], capturar=True)
        except ErrorOperacion as e:
            if "No state file was found" in e.detalle:
                return []   # stack aún no aplicado: no hay recursos
            raise
        return [l for l in salida.splitlines() if l.strip()]

    def salidas(self) -> dict:
        datos = json.loads(self._correr(["output", "-json"], capturar=True) or "{}")
        return {k: v["value"] for k, v in datos.items()}

    def versiones(self) -> dict:
        v = json.loads(self._correr(["version", "-json"], capturar=True))
        # OpenTofu conserva la clave "terraform_version" por compatibilidad
        res = {self.nombre: v.get("terraform_version") or v.get("tofu_version") or v.get("version")}
        lock = self.trabajo / ".terraform.lock.hcl"
        if lock.exists():
            for prov, ver in re.findall(r'provider "([^"]+)" \{\s*version\s*=\s*"([^"]+)"', lock.read_text()):
                res[prov.split("/")[-1]] = ver
        return res


class MotorOpenTofu(MotorTerraform):
    """OpenTofu: usa las MISMAS plantillas HCL (infra/terraform/<stack>) y la misma CLI.

    - Los proveedores hashicorp/* se descargan de registry.opentofu.org (misma versión de API).
    - El backend S3 con use_lockfile (bloqueo nativo) está soportado desde OpenTofu 1.10.
    - Cifrado del estado del lado del cliente (opcional): defina TF_ENCRYPTION en el entorno
      del worker (https://opentofu.org/docs/language/state/encryption/). Si lo activa,
      respalde la clave: sin ella el estado no se puede leer.
    """
    nombre = "opentofu"
    etiqueta = "OpenTofu"
    binario = "tofu"
    candidatos = ("%LOCALAPPDATA%/Microsoft/WinGet/Links/tofu.exe",
                  "%LOCALAPPDATA%/Microsoft/WinGet/Packages/OpenTofu.Tofu_*/tofu.exe",
                  "%ProgramFiles%/OpenTofu/tofu.exe", "C:/ProgramData/chocolatey/bin/tofu.exe",
                  "/usr/local/bin/tofu", "/opt/homebrew/bin/tofu", "/usr/bin/tofu")

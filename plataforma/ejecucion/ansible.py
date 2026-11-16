"""Ejecuta playbooks de Ansible dentro de un contenedor Docker.

Ansible no funciona como nodo de control en Windows. El inventario y la clave SSH se
escriben en un directorio temporal propio de la operación y se borran al terminar.
"""
import json
import re
import shutil
import subprocess
import uuid

from nucleo import config
from nucleo.errores import ErrorOperacion

_imagen_lista = False


def _docker():
    ruta = shutil.which("docker")
    if not ruta:
        raise ErrorOperacion("No se encontró 'docker' en el PATH.")
    return ruta


def docker_disponible() -> bool:
    try:
        r = subprocess.run([_docker(), "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True,
                           timeout=20)
        return r.returncode == 0
    except Exception:
        return False


def preparar_imagen(log):
    global _imagen_lista
    if _imagen_lista:
        return
    if not docker_disponible():
        raise ErrorOperacion("Docker no está en ejecución (error during connect).")
    log("$ docker build (imagen del nodo de control de Ansible)")
    r = subprocess.run([_docker(), "build", "-q", "-t", config.IMAGEN_ANSIBLE, str(config.ANSIBLE_DIR)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ErrorOperacion("No se pudo construir la imagen de Ansible", r.stderr[-3000:])
    _imagen_lista = True


def version_ansible() -> str:
    r = subprocess.run([_docker(), "run", "--rm", config.IMAGEN_ANSIBLE, "ansible", "--version"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.search(r"ansible \[core ([^\]]+)\]", r.stdout)
    return m.group(1) if m else "desconocida"


def inventario_ini(grupos: dict, variables: dict) -> dict:
    """grupos = {"backend": [("nombre", {"ansible_host": ...}), ...]}.

    Las variables NO van en [all:vars]: los group_vars del playbook tienen más precedencia
    y las pisarían. Se pasan como extra-vars (-e @vars.json), la precedencia más alta.
    """
    # Las variables de conexión (ansible_*) van por host: como extra-vars también se aplicarían a
    # localhost (el contenedor), donde el intérprete de Python está en otra ruta.
    conexion = {k: v for k, v in variables.items() if k.startswith("ansible_")}
    variables = {k: v for k, v in variables.items() if not k.startswith("ansible_")}
    lineas = ["# Inventario temporal generado por la plataforma (se borra al terminar)"]
    for grupo, hosts in grupos.items():
        lineas.append(f"[{grupo}]")
        for nombre, hv in hosts:
            lineas.append(nombre + "".join(f" {k}={v}" for k, v in (hv | conexion).items()))
        lineas.append("")
    return {"ini": "\n".join(lineas) + "\n", "vars": variables}


def ejecutar_playbook(playbook: str, inventario: dict, clave_ssh: str, log, progreso=None,
                      total_tareas: int = 60, extra_args: list[str] | None = None):
    """Corre el playbook. progreso(fracción, mensaje) se llama con cada tarea."""
    preparar_imagen(log)
    tmp = config.TMP / uuid.uuid4().hex
    tmp.mkdir(parents=True)
    try:
        (tmp / "inventario.ini").write_text(inventario["ini"], encoding="utf-8")
        (tmp / "vars.json").write_text(json.dumps(inventario["vars"]), encoding="utf-8")
        (tmp / "clave").write_text(clave_ssh, encoding="utf-8", newline="\n")
        cmd_interno = (
            "cp -r /repo/ansible /ansible && chmod -R go-w /ansible && install -m 600 /trabajo/clave /tmp/clave && "
            "cd /ansible && ANSIBLE_CONFIG=/ansible/ansible.cfg ANSIBLE_FORCE_COLOR=0 "
            f"ansible-playbook {playbook} -i /trabajo/inventario.ini -e @/trabajo/vars.json -e repo_dir=/repo "
            "-e ansible_ssh_private_key_file=/tmp/clave " + " ".join(extra_args or []))
        # Solo se montan las carpetas necesarias (no plataforma/datos, que tiene estado y credenciales)
        montajes = []
        for carpeta in (config.ANSIBLE_DIR, config.APP_BACKEND, config.APP_FRONTEND):
            montajes += ["-v", f"{carpeta}:/repo/{carpeta.name}:ro"]
        cmd = [_docker(), "run", "--rm", *montajes, "-v", f"{tmp}:/trabajo:ro",
               config.IMAGEN_ANSIBLE, "bash", "-c", cmd_interno]
        log(f"$ ansible-playbook {playbook}")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace")
        tareas, fallos = 0, []
        for linea in proc.stdout:
            linea = linea.rstrip("\n")
            if not linea.strip() or linea.startswith("FAILED - RETRYING"):
                if linea.startswith("FAILED - RETRYING"):
                    log(linea)
                continue
            log(linea)
            if linea.startswith("TASK [") or linea.startswith("RUNNING HANDLER ["):
                tareas += 1
                if progreso:
                    nombre = re.sub(r"^\w+[ \w]*\[(.*)\].*$", r"\1", linea)
                    progreso(min(tareas / total_tareas, 0.99), nombre)
            if linea.startswith(("fatal:", "failed:")) or "UNREACHABLE!" in linea:
                fallos.append(linea)
        proc.wait()
        if proc.returncode != 0:
            raise ErrorOperacion("La configuración con Ansible falló", "\n".join(fallos[-5:]) or
                                 f"ansible-playbook terminó con código {proc.returncode}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

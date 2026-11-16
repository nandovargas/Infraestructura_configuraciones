"""Worker de la plataforma: ejecuta las operaciones en segundo plano.

- Toma operaciones "en_cola" de la base y las ejecuta (hasta N en paralelo; nunca dos
  sobre el mismo tenant, lo garantiza la cola).
- Cada minuto sincroniza las alertas del Prometheus central; cada 6 h purga snapshots vencidos.
- Si se detuvo a mitad de una operación, al arrancar la marca como fallida (reintentable).

Uso:  python worker.py
"""
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ejecucion import aws, trabajos  # noqa: E402
from nucleo import config, db, monitoreo, servicios  # noqa: E402
from nucleo.errores import ErrorOperacion, traducir  # noqa: E402
from nucleo.seguridad import limpiar  # noqa: E402

LOG_WORKER = config.LOGS / "worker.log"


def log(msg):
    with open(LOG_WORKER, "a", encoding="utf-8") as f:
        f.write(f"{db.ahora()} {limpiar(msg)}\n")
    print(msg, flush=True)


def bloqueo_unico():
    """Impide dos workers a la vez (se libera solo si el proceso muere)."""
    f = open(config.DATOS / "worker.lock", "a+")
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Ya hay un worker en ejecución.")
    return f


def recuperar_interrumpidas():
    for op in db.consultar("SELECT * FROM operaciones WHERE estado = 'en_progreso'"):
        finalizar(op, False, error=("El worker se detuvo durante la operación.",
                                    "Puede reintentarla: el motor retomará desde el estado guardado.", ""))
        log(f"Operación #{op['id']} marcada como interrumpida")


def reclamar():
    with db.transaccion() as con:
        op = con.execute("SELECT * FROM operaciones WHERE estado = 'en_cola' ORDER BY id LIMIT 1").fetchone()
        if not op:
            return None
        con.execute("UPDATE operaciones SET estado = 'en_progreso', iniciado = ?, worker_pid = ? WHERE id = ?",
                    (db.ahora(), os.getpid(), op["id"]))
    return servicios.obtener_operacion(op["id"])


def finalizar(op, exito, resultado=None, error=None):
    t = db.ahora()
    tenant = servicios.obtener_tenant(op["tenant_id"]) if op["tenant_id"] else None
    etiqueta = servicios.TIPOS_OPERACION.get(op["tipo"], op["tipo"])
    if exito:
        db.ejecutar("UPDATE operaciones SET estado = 'exitosa', progreso = 100, paso = 'Completada', resultado = ?,"
                    " terminado = ? WHERE id = ?", (json.dumps(resultado or {}), t, op["id"]))
        if tenant and op["tipo"] in servicios.TRANSICION:
            db.ejecutar("UPDATE tenants SET estado = ?, ultimo_error = '', actualizado = ? WHERE id = ?",
                        (servicios.TRANSICION[op["tipo"]][1], t, tenant["id"]))
        servicios.registrar_evento("operacion", f"{etiqueta}: completada (op #{op['id']})",
                                   tenant_id=op["tenant_id"], entorno=tenant and tenant["entorno"])
    else:
        resumen, sugerencia, detalle = error
        db.ejecutar("UPDATE operaciones SET estado = 'fallida', error_resumen = ?, error_detalle = ?, terminado = ?"
                    " WHERE id = ?", (f"{resumen} {sugerencia}".strip(), detalle, t, op["id"]))
        if tenant:
            db.ejecutar("UPDATE tenants SET estado = 'error', ultimo_error = ?, actualizado = ? WHERE id = ?",
                        (resumen, t, tenant["id"]))
        servicios.registrar_evento("operacion", f"{etiqueta}: falló (op #{op['id']})", resumen,
                                   tenant_id=op["tenant_id"], entorno=tenant and tenant["entorno"],
                                   severidad="warning")


def ejecutar(op):
    ctx = trabajos.Contexto(op)
    ctx.log(f"Operación #{op['id']} · {op['tipo']} · intento {op['intento']}"
            + (f" (reintento de #{op['reintento_de']})" if op["reintento_de"] else ""))
    try:
        resultado = trabajos.MANEJADORES[op["tipo"]](ctx)
        ctx.log("✔ Operación completada")
        finalizar(op, True, resultado)
    except Exception as e:
        detalle = e.detalle if isinstance(e, ErrorOperacion) else traceback.format_exc()
        detalle = ctx.redactor.limpiar(detalle)
        ctx.log(f"✘ {e}\n{detalle}")
        cola = servicios.leer_log(op, ultimas=60)
        mensaje, sugerencia = traducir(f"{e}\n{detalle}\n{cola}",
                                       por_defecto=ctx.redactor.limpiar(getattr(e, "resumen", str(e))))
        finalizar(op, False, error=(mensaje, sugerencia, detalle[-6000:]))
    finally:
        ctx.cerrar()


def tareas_periodicas():
    estado = {}
    while True:
        _periodicas(estado)
        time.sleep(15)


def _periodicas(estado):
    ahora = time.monotonic()
    if ahora - estado.get("alertas", 0) > 60:
        estado["alertas"] = ahora
        try:
            nuevas, resueltas = monitoreo.sincronizar_alertas()
            if nuevas or resueltas:
                log(f"Alertas: {nuevas} nuevas, {resueltas} resueltas")
        except Exception as e:
            log(f"No se pudieron leer las alertas: {e}")
    if ahora - estado.get("snapshots", 0) > 6 * 3600:
        estado["snapshots"] = ahora
        try:
            aws.purgar_snapshots_vencidos(log)
        except Exception as e:
            log(f"No se pudieron purgar snapshots: {e}")


def main():
    _lock = bloqueo_unico()  # noqa: F841 (se mantiene abierto mientras viva el proceso)
    db.inicializar()
    recuperar_interrumpidas()
    log(f"Worker iniciado (pid {os.getpid()}, hasta {config.MAX_TRABAJOS_PARALELOS} operaciones en paralelo)")
    pool = ThreadPoolExecutor(max_workers=config.MAX_TRABAJOS_PARALELOS, thread_name_prefix="op")
    activos = {}
    threading.Thread(target=tareas_periodicas, daemon=True, name="periodicas").start()
    while True:
        db.set_config("worker_latido", {"fecha": db.ahora(), "pid": os.getpid(), "activas": list(activos)})
        for op_id in [i for i, fut in activos.items() if fut.done()]:
            activos.pop(op_id)
        while len(activos) < config.MAX_TRABAJOS_PARALELOS:
            op = reclamar()
            if not op:
                break
            log(f"Ejecutando operación #{op['id']} ({op['tipo']} {op['tenant_id'] or op['objetivo']})")
            activos[op["id"]] = pool.submit(ejecutar, op)
        time.sleep(2)


if __name__ == "__main__":
    main()

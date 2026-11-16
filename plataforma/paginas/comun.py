"""Componentes de interfaz compartidos."""
from datetime import datetime, timezone

import streamlit as st

from nucleo import db, servicios


def fecha(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d/%m/%Y %H:%M:%S")
    except ValueError:
        return iso


def duracion(op) -> str:
    if not op.get("iniciado"):
        return "—"
    fin = datetime.fromisoformat(op["terminado"]) if op.get("terminado") else datetime.now(timezone.utc)
    seg = int((fin - datetime.fromisoformat(op["iniciado"])).total_seconds())
    return f"{seg // 60} min {seg % 60:02d} s"


def aviso_worker():
    latido = db.get_config("worker_latido")
    if not latido:
        st.warning("El worker no se ha iniciado nunca: las operaciones quedarán en cola. "
                   "Ejecute `python worker.py` (o `iniciar.ps1`).", icon="⚠️")
        return
    edad = (datetime.now(timezone.utc) - datetime.fromisoformat(latido["fecha"])).total_seconds()
    if edad > 30:
        st.warning(f"El worker no responde desde hace {int(edad)} s: las operaciones quedarán en cola.", icon="⚠️")


def ficha_plan(plan, compacta=False):
    costo = servicios.estimar_costo(plan)
    limite = f"{plan['max_estudiantes']:,}".replace(",", ".") if plan["max_estudiantes"] else "Sin límite"
    if compacta:
        st.markdown(f"**{plan['nombre']}** · ~USD {costo}/mes")
    else:
        st.markdown(f"#### {plan['nombre']}")
        st.caption(plan["descripcion"])
        st.metric("Costo AWS estimado", f"USD {costo}/mes",
                  help="On-demand us-east-1: cómputo + discos gp3 + IPv4. Suspendido: "
                       f"~USD {servicios.costo_suspendido(plan)}/mes.")
    st.markdown(
        f"""
| Recurso | Asignación |
|---|---|
| Cómputo frontend | `{plan['tipo_frontend']}` |
| Cómputo backend (API) | `{plan['tipo_backend']}` · {plan['gunicorn_threads']} hilos |
| Cómputo base de datos | `{plan['tipo_bd']}` |
| Almacenamiento app | {plan['disco_app_gb']} GB × 2 (gp3 cifrado) |
| Tamaño base de datos | {plan['disco_bd_gb']} GB (gp3 cifrado) |
| Límite de estudiantes por módulo | {limite} |
| Retención de respaldos | {plan['retencion_dias']} días |
""")


def tarjeta_operacion(op_id: int, clave: str):
    """Progreso en vivo de una operación (se refresca solo cada 3 s mientras corre)."""

    @st.fragment(run_every="3s")
    def _vivo():
        op = servicios.obtener_operacion(op_id)
        if not op:
            return
        etiqueta = servicios.TIPOS_OPERACION.get(op["tipo"], op["tipo"])
        st.markdown(f"**#{op['id']} · {etiqueta}** — {servicios.ESTADOS_OPERACION[op['estado']]} · "
                    f"intento {op['intento']} · {duracion(op)}")
        st.progress(op["progreso"] / 100, text=f"{op['progreso']}% · {op['paso'] or 'Esperando al worker…'}")
        if op["estado"] == "fallida":
            st.error(op["error_resumen"], icon="🚫")
        with st.expander("Registro en vivo (últimas líneas)", expanded=op["estado"] == "en_progreso"):
            st.code(servicios.leer_log(op, ultimas=25) or "(sin registro aún)", language="log")
        if op["estado"] in ("exitosa", "fallida") and st.session_state.get(f"{clave}_terminada") != op["estado"]:
            st.session_state[f"{clave}_terminada"] = op["estado"]
            st.rerun()  # refresca la página completa al terminar (estado del tenant, botones)

    _vivo()

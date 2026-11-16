import json

import pandas as pd
import streamlit as st

from ejecucion import consultas
from motores import MOTORES
from nucleo import db, monitoreo, servicios
from nucleo.seguridad import limpiar

from .comun import aviso_worker, duracion, fecha, ficha_plan, tarjeta_operacion
from .monitoreo import tabla_metricas


def pagina():
    st.title("🔎 Detalle de tenant")
    aviso_worker()
    tenants = servicios.listar_tenants(incluir_eliminados=True)
    if not tenants:
        st.info("Todavía no hay tenants. Cree uno en «Tenants».")
        return
    ids = [t["id"] for t in tenants]
    actual = st.session_state.get("tenant_sel")
    tid = st.selectbox("Tenant", ids, index=ids.index(actual) if actual in ids else 0,
                       format_func=lambda i: next(f"{t['nombre']} ({i}) · {servicios.ESTADOS_TENANT[t['estado']]}"
                                                  for t in tenants if t["id"] == i))
    st.session_state["tenant_sel"] = tid
    t = servicios.obtener_tenant(tid)
    plan = servicios.obtener_plan(t["plan_id"])
    s = t["salidas"]

    # ---------- Cabecera ----------
    c = st.columns([2, 1, 1, 1, 1])
    c[0].markdown(f"## {t['nombre']}\n`{t['id']}` · contacto: {t['contacto_nombre']} · {t['contacto_email']}"
                  + (f" · {t['contacto_telefono']}" if t["contacto_telefono"] else ""))
    c[1].metric("Estado", servicios.ESTADOS_TENANT[t["estado"]])
    c[2].metric("Plan", plan["nombre"])
    c[3].metric("Entorno", t["entorno"])
    c[4].metric("Motor", MOTORES[t["motor"]].etiqueta if t["motor"] in MOTORES else t["motor"])
    if t["estado"] == "error" and t["ultimo_error"]:
        st.error(f"Último error: {t['ultimo_error']}", icon="🚫")

    if s.get("frontend_url") and t["estado"] != "eliminado":
        m = db.get_config("monitoreo")
        enlaces = st.columns(4)
        enlaces[0].link_button("🌐 Aplicación (frontend)", s["frontend_url"], width="stretch")
        enlaces[1].link_button("🔐 Panel administrador", s["frontend_url"] + "/panel/login", width="stretch")
        enlaces[2].link_button("🔌 API", s["backend_url"] + "/modulo-activo/", width="stretch")
        if m:
            enlaces[3].link_button("📊 Grafana del tenant", f"{m['grafana_url']}/d/asistencia-tenants?"
                                   f"var-tenant={t['id']}&var-entorno={t['entorno']}", width="stretch")
        else:
            enlaces[3].link_button("📈 Prometheus del tenant", s["prometheus_url"], width="stretch")

    # ---------- Operación en curso / última ----------
    ops = servicios.listar_operaciones(tenant_id=tid, limite=50)
    if ops:
        ultima = ops[0]
        with st.container(border=True):
            tarjeta_operacion(ultima["id"], f"det_{tid}")
            if ultima["estado"] == "fallida":
                with st.expander("Detalle técnico del error (sin secretos)"):
                    st.code(limpiar(ultima["error_detalle"]) or "—", language="log")
                if st.button("🔁 Reintentar operación", type="primary", key="reintentar"):
                    try:
                        servicios.reintentar(ultima["id"])
                        st.rerun()
                    except servicios.ErrorValidacion as e:
                        st.error(str(e))
                st.caption("Reintentar es seguro: Terraform parte del estado guardado y solo crea lo que falta; "
                           "Ansible es idempotente.")

    pestañas = st.tabs(["⚙️ Ciclo de vida", "🔑 Credenciales", "📈 Salud", "🚀 Despliegues",
                        "📜 Operaciones", "💾 Respaldos", "🗑️ Eliminar"])
    permitidas = servicios.acciones_permitidas(t)

    with pestañas[0]:
        ciclo_de_vida(t, plan, permitidas)
    with pestañas[1]:
        credenciales(t)
    with pestañas[2]:
        salud(t)
    with pestañas[3]:
        despliegues(tid)
    with pestañas[4]:
        st.dataframe(pd.DataFrame([{
            "#": o["id"], "Operación": servicios.TIPOS_OPERACION.get(o["tipo"], o["tipo"]),
            "Estado": servicios.ESTADOS_OPERACION[o["estado"]], "Intento": o["intento"],
            "Creada": fecha(o["creado"]), "Duración": duracion(o), "Error": o["error_resumen"],
        } for o in ops]), hide_index=True, width="stretch") if ops else st.caption("Sin operaciones.")
    with pestañas[5]:
        respaldos(t)
    with pestañas[6]:
        eliminar(t, plan, permitidas)


def _accion(t, accion, etiqueta, **params):
    try:
        op = servicios.solicitar(t["id"], accion, **params)
        st.toast(f"{etiqueta}: operación #{op} en cola", icon="🚀")
        st.rerun()
    except servicios.ErrorValidacion as e:
        st.error(str(e))


def ciclo_de_vida(t, plan, permitidas):
    if not permitidas:
        st.info("Hay una operación en curso o el estado actual no admite acciones." if t["estado"] != "eliminado"
                else "Este tenant fue eliminado.")
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("#### 🚀 Desplegar / actualizar")
        st.caption("Aplica la versión actual de la aplicación y de las plantillas, y corrige cualquier desvío "
                   "de la infraestructura. Es seguro repetirlo.")
        if st.button("Desplegar ahora", disabled="desplegar" not in permitidas, width="stretch"):
            _accion(t, "desplegar", "Despliegue")
    with b.container(border=True):
        st.markdown("#### 📦 Cambiar de plan")
        otros = [p for p in servicios.listar_planes(solo_activos=True) if p["id"] != t["plan_id"]]
        if otros:
            nuevo = st.selectbox("Nuevo plan", [p["id"] for p in otros],
                                 format_func=lambda i: next(p["nombre"] for p in otros if p["id"] == i))
            with st.expander("Características del nuevo plan"):
                ficha_plan(next(p for p in otros if p["id"] == nuevo), compacta=True)
            st.caption("Cambia el tamaño de las instancias (se reinician unos minutos) y amplía discos. "
                       "Los discos no se pueden reducir.")
            if st.button("Cambiar plan y redesplegar", disabled="desplegar" not in permitidas,
                         width="stretch"):
                _accion(t, "desplegar", "Cambio de plan", plan_id=nuevo)
    a, b = st.columns(2)
    with a.container(border=True):
        st.markdown("#### ⏸️ Suspender")
        st.markdown(f"""**Política de suspensión**
- Se toma un **snapshot de la base de datos** que se conserva **{plan['retencion_dias']} días** (plan {plan['nombre']}).
- Se **detienen las 3 instancias**: la aplicación deja de estar disponible.
- Se **conservan** discos, datos, IP fijas y credenciales: al reanudar todo vuelve igual.
- Costo mientras está suspendido: ~USD {servicios.costo_suspendido(plan)}/mes (en lugar de ~USD {servicios.estimar_costo(plan)}).""")
        if st.button("Suspender tenant", disabled="suspender" not in permitidas, width="stretch"):
            _accion(t, "suspender", "Suspensión")
    with b.container(border=True):
        st.markdown("#### ▶️ Reanudar")
        st.caption("Enciende las instancias y verifica de punta a punta: frontend, login en la API y métricas.")
        if st.button("Reanudar tenant", disabled="reanudar" not in permitidas, width="stretch"):
            _accion(t, "reanudar", "Reanudación")


def credenciales(t):
    st.caption("Las credenciales se leen del estado cifrado de Terraform solo al pedirlas; "
               "la plataforma no las guarda en su base de datos ni en los registros.")
    if t["estado"] in ("eliminado", "pendiente"):
        st.info("No disponible en este estado.")
        return
    if st.button("Mostrar credenciales del administrador de la aplicación"):
        with st.spinner("Leyendo el estado de Terraform…"):
            try:
                cred = consultas.credenciales_app(t["id"])
                st.text_input("Usuario", cred["usuario"], disabled=True)
                st.text_input("Contraseña", cred["password"] or "(no disponible)", type="password")
                st.caption("Use el ojo del campo para verla. Entréguela por un canal seguro a la institución.")
            except Exception as e:
                st.error(f"No se pudieron leer las credenciales: {limpiar(str(e))}")


def salud(t):
    if t["estado"] != "activo":
        st.info("Las métricas están disponibles cuando el tenant está activo.")
        return
    m = db.get_config("monitoreo")
    if m:
        url, central = m["prometheus_url"], True
        st.caption("Fuente: Prometheus central (instancia de monitoreo)")
    else:
        url, central = t["salidas"].get("prometheus_url"), False
        st.caption("Fuente: Prometheus propio del tenant (despliegue el monitoreo para alertas y Grafana)")
    tabla_metricas(url, monitoreo.filtro(t["id"], [t["entorno"]], central))
    st.markdown("##### Alertas y eventos de este tenant")
    ev = servicios.listar_eventos(tenant_id=t["id"], limite=30)
    if ev:
        st.dataframe(pd.DataFrame([{"Detectado": fecha(e["detectado_en"]), "Tipo": e["origen"],
                                    "Severidad": e["severidad"], "Evento": e["titulo"], "Estado": e["estado"],
                                    "Resuelto": fecha(e["resuelto_en"])} for e in ev]),
                     hide_index=True, width="stretch")
    else:
        st.caption("Sin eventos.")


def despliegues(tid):
    filas = servicios.listar_despliegues(tid)
    if not filas:
        st.caption("Sin despliegues registrados.")
        return
    st.dataframe(pd.DataFrame([{
        "#": d["id"], "Fecha": fecha(d["creado"]), "Operación": d["operacion_id"], "Resultado": d["resultado"],
        "App backend": d["version_backend"], "App frontend": d["version_frontend"],
        "Plantillas": d["version_plantillas"], "Plan": json.loads(d["plan"]).get("id", ""),
    } for d in filas]), hide_index=True, width="stretch")
    sel = st.selectbox("Ver herramientas y AMIs del despliegue", [d["id"] for d in filas])
    d = next(x for x in filas if x["id"] == sel)
    a, b = st.columns(2)
    a.markdown("**Herramientas**")
    a.json(json.loads(d["herramientas"]))
    b.markdown("**AMIs (Packer) y plan**")
    b.json({"amis": json.loads(d["amis"]), "plan": json.loads(d["plan"])})


def respaldos(t):
    st.caption("Snapshots de la base de datos creados al suspender o eliminar. Se borran solos al vencer.")
    if st.button("Consultar snapshots en AWS"):
        with st.spinner("Consultando AWS…"):
            try:
                snaps = consultas.snapshots(t["id"])
                if snaps:
                    st.dataframe(pd.DataFrame(snaps), hide_index=True, width="stretch")
                else:
                    st.info("No hay snapshots de este tenant.")
            except Exception as e:
                st.error(f"No se pudo consultar AWS: {limpiar(str(e))}")


def eliminar(t, plan, permitidas):
    if "eliminar" not in permitidas:
        st.info("No se puede eliminar en este momento (estado actual u operación en curso).")
        return
    st.warning("La eliminación es **definitiva**. Primero revise qué se eliminará.", icon="⚠️")
    clave = f"preview_{t['id']}"
    if st.button("1️⃣ Analizar qué se eliminará"):
        with st.spinner("Leyendo el estado de Terraform del tenant…"):
            try:
                st.session_state[clave] = consultas.vista_previa_eliminacion(t["id"])
            except Exception as e:
                st.error(f"No se pudo leer el estado: {limpiar(str(e))}")
    prev = st.session_state.get(clave)
    if not prev:
        return
    st.markdown(f"#### Recursos de AWS que se eliminarán ({len(prev['recursos'])})")
    if prev["recursos"]:
        st.dataframe(pd.DataFrame(prev["recursos"]), hide_index=True, width="stretch")
    else:
        st.info("El tenant no tiene recursos creados (nunca se desplegó o ya se eliminaron).")
    if prev["datos"]:
        st.markdown("#### Datos que se perderán")
        st.markdown("\n".join(f"- {d}" for d in prev["datos"]))
    st.markdown("#### Se conserva")
    st.markdown("\n".join(f"- {d}" for d in prev["conserva"]))

    conservar = st.checkbox(f"Crear snapshot final de la base de datos y conservarlo {plan['retencion_dias']} días",
                            value=True)
    confirmacion = st.text_input(f"2️⃣ Para confirmar, escriba el identificador del tenant: **{t['id']}**")
    if st.button("🗑️ Eliminar definitivamente", type="primary", disabled=confirmacion.strip() != t["id"]):
        st.session_state.pop(clave, None)
        _accion(t, "eliminar", "Eliminación", conservar_snapshot=conservar)

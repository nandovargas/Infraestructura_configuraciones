import json

import pandas as pd
import streamlit as st

from motores import MOTORES
from nucleo import config, db, servicios

from . import analisis, navegacion
from .comun import aviso_worker, fecha, ficha_plan


def pagina():
    st.title("🏫 Tenants")
    aviso_worker()
    tab_lista, tab_nuevo = st.tabs(["Consultar tenants", "➕ Nuevo tenant"])
    with tab_lista:
        lista()
    with tab_nuevo:
        nuevo()


def lista():
    planes = {p["id"]: p["nombre"] for p in servicios.listar_planes()}
    a, b, c, d = st.columns([2, 2, 2, 1.4])
    texto = a.text_input("Buscar por nombre o identificador", placeholder="ej. colegio")
    estados = b.multiselect("Estado", list(servicios.ESTADOS_TENANT), format_func=servicios.ESTADOS_TENANT.get)
    plan_sel = c.multiselect("Plan", list(planes), format_func=planes.get)
    entornos = d.multiselect("Entorno", config.ENTORNOS)
    tenants = servicios.listar_tenants(texto, estados, plan_sel, entornos)
    if not tenants:
        st.info("No hay tenants que coincidan con los filtros.")
        return
    filas = []
    for t in tenants:
        s = json.loads(t["salidas"] or "{}")
        filas.append({
            "ID": t["id"], "Institución": t["nombre"], "Estado": servicios.ESTADOS_TENANT[t["estado"]],
            "Plan": t["plan_nombre"], "Entorno": t["entorno"], "Motor": t["motor"],
            "Frontend": s.get("frontend_url", ""), "Contacto": t["contacto_email"],
            "Creado": fecha(t["creado"]),
        })
    evento = st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch",
                          on_select="rerun", selection_mode="single-row",
                          column_config={"Frontend": st.column_config.LinkColumn()})
    st.caption(f"{len(filas)} tenant(s). Seleccione una fila para ver el detalle.")
    if evento.selection.rows:
        navegacion.ir_a_tenant(filas[evento.selection.rows[0]]["ID"])


def nuevo():
    planes = servicios.listar_planes(solo_activos=True)
    if not planes:
        st.warning("No hay planes activos. Cree uno en «Planes».")
        return
    backend = db.get_config("estado_tipo", "local")
    if backend == "local":
        st.info("El estado de la infraestructura se guardará **localmente** (modo desarrollo). Para producción, "
                "prepare el bucket S3 cifrado en «Infraestructura».", icon="ℹ️")

    st.markdown("##### 1. Elija un plan")
    ids = [p["id"] for p in planes]
    plan_id = st.radio("Plan", ids, horizontal=True, format_func=lambda i: next(p["nombre"] for p in planes
                                                                                  if p["id"] == i),
                       label_visibility="collapsed")
    with st.container(border=True):
        ficha_plan(next(p for p in planes if p["id"] == plan_id))

    st.markdown("##### 2. Elija el motor de aprovisionamiento")
    elegibles = [m for m in MOTORES if MOTORES[m].disponible and MOTORES[m].soporta("tenant")]
    motor = st.radio("Motor de aprovisionamiento", elegibles, horizontal=True, format_func=_etiqueta_motor,
                     label_visibility="collapsed")
    st.caption("Terraform y OpenTofu usan las mismas plantillas HCL (`infra/terraform/tenant`); Pulumi usa el "
               "programa Python equivalente (`infra/pulumi/tenant`). Los tres crean la misma infraestructura y el "
               "motor queda fijo para el tenant.")

    st.markdown("##### 3. Revise la seguridad del código que se va a desplegar")
    analisis.panel_motor(motor)

    st.markdown("##### 4. Datos del tenant")
    with st.form("nuevo_tenant", clear_on_submit=False):
        a, b = st.columns(2)
        nombre = a.text_input("Nombre de la institución *", placeholder="Colegio Andino")
        ident = b.text_input("Identificador único *", placeholder="colegio-andino",
                             help="3-21 caracteres: minúsculas, números y guiones. Se usa en los nombres de AWS y en la URL del tenant: "
                                  + config.url_tenant("<id>", "<IP>") + "/panel/login")
        a, b, c = st.columns(3)
        contacto = a.text_input("Persona de contacto *")
        email = b.text_input("Correo de contacto *")
        telefono = c.text_input("Teléfono")
        entorno = st.selectbox("Entorno", config.ENTORNOS)
        st.caption("El despliegue crea 3 instancias EC2 dedicadas (frontend, backend, base de datos), sus grupos de "
                   "seguridad, IP fijas y la base de datos propia del tenant, y configura la app con Ansible.")
        if st.form_submit_button(f"Crear y desplegar tenant con {MOTORES[motor].etiqueta}", type="primary"):
            if not MOTORES[motor].disponible or not MOTORES[motor].soporta("tenant"):
                st.error(f"{MOTORES[motor].etiqueta} todavía no está disponible. "
                         "Use Terraform, OpenTofu o Pulumi.")
                return
            try:
                op = servicios.crear_tenant(dict(id=ident.strip().lower(), nombre=nombre, contacto_nombre=contacto,
                                                 contacto_email=email, contacto_telefono=telefono, entorno=entorno,
                                                 plan_id=plan_id, motor=motor))
                st.toast(f"Tenant creado. Despliegue en cola (operación #{op}).", icon="🚀")
                navegacion.ir_a_tenant(ident.strip().lower())
            except servicios.ErrorValidacion as e:
                for linea in str(e).splitlines():
                    st.error(linea)


def _etiqueta_motor(m):
    clase = MOTORES[m]
    if not clase.disponible or not clase.soporta("tenant"):
        return f"{clase.etiqueta} (próximamente)"
    return clase.etiqueta + ("" if clase.instalado() else " (no instalado)")

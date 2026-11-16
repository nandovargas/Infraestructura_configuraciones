"""Plataforma de tenants de Asistencia · interfaz de administración (Streamlit).

Uso:  streamlit run app.py     (el worker se inicia aparte: python worker.py)
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nucleo import db, seguridad  # noqa: E402
from paginas import (analisis, monitoreo, navegacion, operaciones, planes, plataforma, resumen,  # noqa: E402
                     tenant_detalle, tenants)

st.set_page_config(page_title="Asistencia SaaS", page_icon="🏫", layout="wide")
db.inicializar()


def primer_uso():
    st.title("🏫 Asistencia SaaS · configuración inicial")
    st.info("Cree el usuario administrador de la plataforma. La contraseña se guarda como hash scrypt en "
            "`plataforma/datos/admin.json` (fuera del control de versiones).")
    with st.form("crear_admin"):
        usuario = st.text_input("Usuario", value="admin")
        c1 = st.text_input("Contraseña", type="password")
        c2 = st.text_input("Repita la contraseña", type="password")
        if st.form_submit_button("Crear administrador", type="primary"):
            if len(c1) < 10:
                st.error("Use al menos 10 caracteres.")
            elif c1 != c2:
                st.error("Las contraseñas no coinciden.")
            else:
                seguridad.crear_admin(usuario.strip(), c1)
                st.success("Administrador creado. Inicie sesión.")
                st.rerun()


def login():
    st.title("🏫 Asistencia SaaS")
    st.caption("Plataforma de creación y administración de tenants")
    with st.form("login"):
        usuario = st.text_input("Usuario")
        clave = st.text_input("Contraseña", type="password")
        if st.form_submit_button("Ingresar", type="primary"):
            if seguridad.verificar_admin(usuario, clave):
                st.session_state["usuario"] = usuario
                st.rerun()
            else:
                st.error("Usuario o contraseña incorrectos.")


if not seguridad.admin_configurado():
    primer_uso()
elif "usuario" not in st.session_state:
    login()
else:
    navegacion.PAGINAS.update({
        "resumen": st.Page(resumen.pagina, title="Resumen", icon="🏠", url_path="resumen", default=True),
        "planes": st.Page(planes.pagina, title="Planes", icon="📦", url_path="planes"),
        "tenants": st.Page(tenants.pagina, title="Tenants", icon="🏫", url_path="tenants"),
        "tenant": st.Page(tenant_detalle.pagina, title="Detalle de tenant", icon="🔎", url_path="tenant"),
        "operaciones": st.Page(operaciones.pagina, title="Operaciones", icon="⚙️", url_path="operaciones"),
        "monitoreo": st.Page(monitoreo.pagina, title="Salud y alertas", icon="📈", url_path="monitoreo"),
        "seguridad": st.Page(analisis.pagina, title="Seguridad IaC", icon="🛡️", url_path="seguridad"),
        "infraestructura": st.Page(plataforma.pagina, title="Infraestructura", icon="🛠️",
                                   url_path="infraestructura"),
    })
    nav = st.navigation(list(navegacion.PAGINAS.values()))
    with st.sidebar:
        st.caption(f"Sesión: **{st.session_state['usuario']}**")
        if st.button("Cerrar sesión"):
            st.session_state.clear()
            st.rerun()
    nav.run()

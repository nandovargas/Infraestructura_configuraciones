import pandas as pd
import streamlit as st

from nucleo import servicios

from .comun import ficha_plan


def pagina():
    st.title("📦 Catálogo de planes")
    planes = servicios.listar_planes()

    cols = st.columns(max(len([p for p in planes if p["activo"]]), 1))
    for col, p in zip(cols, [p for p in planes if p["activo"]]):
        with col.container(border=True):
            ficha_plan(p)
            st.caption(f"Tenants usando este plan: {servicios.tenants_con_plan(p['id'])}")

    st.divider()
    st.subheader("Administrar planes")
    st.dataframe(pd.DataFrame([{
        "ID": p["id"], "Nombre": p["nombre"], "Activo": bool(p["activo"]), "Frontend": p["tipo_frontend"],
        "Backend": p["tipo_backend"], "BD": p["tipo_bd"], "Disco app (GB)": p["disco_app_gb"],
        "Disco BD (GB)": p["disco_bd_gb"], "Máx. estudiantes": p["max_estudiantes"],
        "Hilos API": p["gunicorn_threads"], "Retención (días)": p["retencion_dias"],
        "USD/mes": servicios.estimar_costo(p),
    } for p in planes]), hide_index=True, width="stretch")

    opciones = ["➕ Nuevo plan"] + [p["id"] for p in planes]
    elegido = st.selectbox("Plan a crear o editar", opciones)
    nuevo = elegido == opciones[0]
    p = {} if nuevo else servicios.obtener_plan(elegido)
    if not nuevo and servicios.tenants_con_plan(elegido):
        st.info("Los cambios se aplican a los tenants de este plan en su próximo despliegue o actualización.")

    with st.form("plan"):
        a, b = st.columns(2)
        ident = a.text_input("Identificador", value=p.get("id", ""), disabled=not nuevo,
                             help="Minúsculas, números y guiones. No se puede cambiar después.")
        nombre = b.text_input("Nombre", value=p.get("nombre", ""))
        descripcion = st.text_area("Descripción", value=p.get("descripcion", ""), height=70)
        st.markdown("**Capacidad de cómputo**")
        a, b, c = st.columns(3)
        tipos = servicios.TIPOS_INSTANCIA
        tf = a.selectbox("Frontend", tipos, index=tipos.index(p.get("tipo_frontend", "t3.micro")))
        tb = b.selectbox("Backend", tipos, index=tipos.index(p.get("tipo_backend", "t3.small")))
        td = c.selectbox("Base de datos", tipos, index=tipos.index(p.get("tipo_bd", "t3.small")))
        st.markdown("**Almacenamiento y límites de uso**")
        a, b, c, d, e = st.columns(5)
        disco_app = a.number_input("Disco app (GB)", 16, 500, p.get("disco_app_gb", 16))
        disco_bd = b.number_input("Tamaño BD (GB)", 16, 2000, p.get("disco_bd_gb", 20))
        max_est = c.number_input("Máx. estudiantes", 0, 1_000_000, p.get("max_estudiantes", 500),
                                 help="Por módulo. 0 = sin límite. Lo aplica la API al importar.")
        hilos = d.number_input("Hilos de la API", 1, 64, p.get("gunicorn_threads", 4))
        retencion = e.number_input("Retención (días)", 1, 365, p.get("retencion_dias", 7),
                                   help="Días que se conservan los snapshots de la BD al suspender o eliminar.")
        activo = st.checkbox("Disponible para nuevos tenants", value=bool(p.get("activo", 1)))
        if st.form_submit_button("Guardar plan", type="primary"):
            datos = dict(id=ident.strip() if nuevo else p["id"], nombre=nombre, descripcion=descripcion,
                         tipo_frontend=tf, tipo_backend=tb, tipo_bd=td, disco_app_gb=int(disco_app),
                         disco_bd_gb=int(disco_bd), max_estudiantes=int(max_est), gunicorn_threads=int(hilos),
                         retencion_dias=int(retencion), activo=int(activo))
            try:
                servicios.guardar_plan(datos, nuevo)
                st.success("Plan guardado.")
                st.rerun()
            except servicios.ErrorValidacion as e:
                st.error(str(e))

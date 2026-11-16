import pandas as pd
import streamlit as st

from nucleo import db, servicios

from .comun import aviso_worker, fecha


def pagina():
    st.title("🏠 Resumen")
    aviso_worker()

    tenants = servicios.listar_tenants()
    en_curso = servicios.listar_operaciones(estados=["en_cola", "en_progreso"])
    alertas = servicios.listar_eventos(solo_alertas=True, solo_activas=True)
    fallidas = db.uno("SELECT count(*) n FROM operaciones WHERE estado = 'fallida' "
                      "AND terminado > datetime('now', '-1 day')")["n"]

    c = st.columns(5)
    c[0].metric("Tenants", len(tenants))
    c[1].metric("Activos", sum(t["estado"] == "activo" for t in tenants))
    c[2].metric("Suspendidos", sum(t["estado"] == "suspendido" for t in tenants))
    c[3].metric("Operaciones en curso", len(en_curso))
    c[4].metric("Alertas activas", len(alertas), delta=f"{fallidas} op. fallidas (24 h)" if fallidas else None,
                delta_color="inverse")

    if tenants:
        st.subheader("Tenants por estado, plan y entorno")
        df = pd.DataFrame(tenants)
        a, b, c_ = st.columns(3)
        a.bar_chart(df["estado"].map(servicios.ESTADOS_TENANT).value_counts(), horizontal=True)
        b.bar_chart(df["plan_nombre"].value_counts(), horizontal=True)
        c_.bar_chart(df["entorno"].value_counts(), horizontal=True)

    izq, der = st.columns(2)
    with izq:
        st.subheader("🚨 Alertas y eventos recientes")
        eventos = servicios.listar_eventos(limite=15)
        if eventos:
            st.dataframe(pd.DataFrame([{
                "Detectado": fecha(e["detectado_en"]),
                "Tenant": e["tenant_id"] or "plataforma",
                "Tipo": {"alerta": "🚨 Alerta", "operacion": "⚙️ Operación"}.get(e["origen"], "🛠️ Plataforma"),
                "Severidad": e["severidad"],
                "Evento": e["titulo"],
                "Estado": {"activa": "🔴 activa", "resuelta": "✅ resuelta"}.get(e["estado"], ""),
            } for e in eventos]), hide_index=True, width="stretch")
        else:
            st.caption("Sin eventos todavía.")
    with der:
        st.subheader("⚙️ Operaciones recientes")
        ops = servicios.listar_operaciones(limite=15)
        if ops:
            st.dataframe(pd.DataFrame([{
                "#": o["id"], "Creada": fecha(o["creado"]), "Recurso": o["tenant_id"] or o["objetivo"],
                "Operación": servicios.TIPOS_OPERACION.get(o["tipo"], o["tipo"]),
                "Estado": servicios.ESTADOS_OPERACION[o["estado"]], "Progreso": o["progreso"],
            } for o in ops]), hide_index=True, width="stretch",
                column_config={"Progreso": st.column_config.ProgressColumn(min_value=0, max_value=100)})
        else:
            st.caption("Sin operaciones todavía.")

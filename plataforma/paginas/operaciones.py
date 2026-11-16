import pandas as pd
import streamlit as st

from nucleo import servicios
from nucleo.seguridad import limpiar

from .comun import aviso_worker, duracion, fecha, tarjeta_operacion


def pagina():
    st.title("⚙️ Operaciones (trabajos en segundo plano)")
    aviso_worker()
    a, b = st.columns([2, 3])
    estados = a.multiselect("Estado", list(servicios.ESTADOS_OPERACION), format_func=servicios.ESTADOS_OPERACION.get)
    tenant = b.text_input("Tenant (identificador exacto)")
    ops = servicios.listar_operaciones(tenant_id=tenant.strip() or None, estados=estados)
    if not ops:
        st.info("No hay operaciones.")
        return
    filas = [{
        "#": o["id"], "Recurso": o["tenant_id"] or o["objetivo"],
        "Operación": servicios.TIPOS_OPERACION.get(o["tipo"], o["tipo"]),
        "Estado": servicios.ESTADOS_OPERACION[o["estado"]], "Progreso": o["progreso"], "Paso": o["paso"],
        "Intento": o["intento"], "Creada": fecha(o["creado"]), "Duración": duracion(o),
    } for o in ops]
    sel = st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch", on_select="rerun",
                       selection_mode="single-row",
                       column_config={"Progreso": st.column_config.ProgressColumn(min_value=0, max_value=100)})
    op_id = filas[sel.selection.rows[0]]["#"] if sel.selection.rows else ops[0]["id"]

    st.divider()
    op = servicios.obtener_operacion(op_id)
    tarjeta_operacion(op_id, f"ops_{op_id}")
    c1, c2 = st.columns(2)
    c1.markdown("**Parámetros**")
    c1.json(op["parametros"])
    c2.markdown("**Resultado**")
    c2.json(op["resultado"])
    if op["estado"] == "fallida":
        st.markdown("**Detalle técnico del error** (los secretos se ocultan)")
        st.code(limpiar(op["error_detalle"]) or "—", language="log")
        if st.button("🔁 Reintentar", type="primary"):
            try:
                nuevo = servicios.reintentar(op_id)
                st.toast(f"Reintento en cola: operación #{nuevo}", icon="🔁")
                st.rerun()
            except servicios.ErrorValidacion as e:
                st.error(str(e))
    with st.expander("Registro completo"):
        log = servicios.leer_log(op)
        st.code(log or "(vacío)", language="log")
        st.download_button("Descargar registro", log, file_name=f"operacion-{op_id}.log")

import json

import pandas as pd
import streamlit as st

from ejecucion import consultas
from motores import MOTORES
from nucleo import config, db, servicios
from nucleo.seguridad import limpiar

from .comun import aviso_worker, tarjeta_operacion


def _ultima(objetivo, tipos):
    ops = [o for o in servicios.listar_operaciones(limite=300) if o["objetivo"] == objetivo and o["tipo"] in tipos]
    return ops[0] if ops else None


def _encolar(objetivo, tipo):
    try:
        op = servicios.encolar(objetivo, tipo)
        st.toast(f"Operación #{op} en cola", icon="🚀")
        st.rerun()
    except servicios.ErrorValidacion as e:
        st.error(str(e))


def pagina():
    st.title("🛠️ Infraestructura de la plataforma")
    aviso_worker()

    # ---------- Monitoreo ----------
    st.subheader("📊 Monitoreo compartido (Grafana + Prometheus central)")
    m = db.get_config("monitoreo")
    ultima = _ultima("monitoreo", ("desplegar_monitoreo", "eliminar_monitoreo"))
    if m:
        a, b = st.columns(2)
        a.link_button("Abrir Grafana", m["grafana_url"], width="stretch")
        b.link_button("Abrir Prometheus central", m["prometheus_url"] + "/alerts", width="stretch")
        if st.button("Mostrar credenciales de Grafana"):
            try:
                cred = consultas.credenciales_grafana()
                st.text_input("Usuario Grafana", cred["usuario"], disabled=True)
                st.text_input("Contraseña Grafana", cred["password"], type="password")
            except Exception as e:
                st.error(limpiar(str(e)))
        sin_conectar = [t["id"] for t in servicios.listar_tenants(estados=["activo"])
                        if not json.loads(t["salidas"] or "{}").get("monitoreo_conectado")]
        if sin_conectar:
            st.warning(f"Tenants activos cuyo Prometheus aún no está abierto al monitoreo: {', '.join(sin_conectar)}")
            if st.button("Conectarlos (redesplegar)"):
                for tid in sin_conectar:
                    try:
                        servicios.solicitar(tid, "desplegar")
                    except servicios.ErrorValidacion as e:
                        st.error(f"{tid}: {e}")
                st.rerun()
    else:
        st.info("Instancia EC2 separada con Grafana y un Prometheus central que descubre automáticamente el "
                "Prometheus del backend de cada tenant (por etiquetas EC2), federa sus métricas y evalúa las alertas.")
    c1, c2 = st.columns(2)
    if c1.button("Desplegar / actualizar monitoreo", type="primary", width="stretch"):
        _encolar("monitoreo", "desplegar_monitoreo")
    if m and c2.button("Eliminar monitoreo", width="stretch"):
        _encolar("monitoreo", "eliminar_monitoreo")
    if ultima:
        with st.container(border=True):
            tarjeta_operacion(ultima["id"], "monitoreo")

    st.divider()
    # ---------- Estado ----------
    st.subheader("🗄️ Almacenamiento del estado (Terraform, OpenTofu y Pulumi)")
    tipo = db.get_config("estado_tipo", "local")
    if tipo == "s3":
        st.success(f"Los nuevos stacks guardan su estado en **s3://{db.get_config('estado_bucket')}/** "
                   "(cifrado KMS, versionado, sin acceso público, solo TLS, bloqueo nativo con `use_lockfile`).")
    else:
        st.warning(f"Modo desarrollo: el estado se guarda en `{config.ESTADO_LOCAL}` en esta máquina. "
                   "Contiene secretos de los tenants: proteja la carpeta y haga copias de seguridad, "
                   "o prepare el bucket S3.")
        if st.button("Preparar almacenamiento S3 del estado"):
            _encolar("estado", "preparar_estado")
    ultima = _ultima("estado", ("preparar_estado",))
    if ultima:
        with st.container(border=True):
            tarjeta_operacion(ultima["id"], "estado")
    tenants_local = [t["id"] for t in servicios.listar_tenants()
                     if json.loads(t["backend_estado"])["tipo"] == "local"]
    if tipo == "s3" and tenants_local:
        st.caption(f"Tenants creados antes con estado local (lo conservan): {', '.join(tenants_local)}")

    st.divider()
    st.subheader("ℹ️ Entorno")
    latido = db.get_config("worker_latido") or {}
    st.json({"region": config.REGION, "datos": str(config.DATOS), "worker": latido,
             "trabajos_paralelos": config.MAX_TRABAJOS_PARALELOS,
             "plantillas_hcl": str(config.PLANTILLAS_TF), "plantillas_pulumi": str(config.PLANTILLAS_PULUMI),
             "ansible": str(config.ANSIBLE_DIR)})

    st.subheader("⚙️ Motores de aprovisionamiento")
    st.dataframe(pd.DataFrame([{
        "Motor": c.etiqueta,
        "Disponible": "Sí" if c.disponible else "Próximamente",
        "Para tenants": "Sí" if c.disponible and c.soporta("tenant") else "No",
        "Plantillas": str(c.dir_plantillas) if c.disponible else "—",
        "Binario": (c.ruta_binario() or "no instalado") if c.binario else "—",
    } for c in MOTORES.values()]), hide_index=True, width="stretch")
    st.caption("El monitoreo y el bucket del estado se aprovisionan siempre con Terraform. "
               "Pulumi cifra los secretos de su estado con la frase de `datos/pulumi.frase` "
               "(o PULUMI_CONFIG_PASSPHRASE): respáldela junto con el estado.")

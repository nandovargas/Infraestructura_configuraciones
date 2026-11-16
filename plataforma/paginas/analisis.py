"""Seguridad del código de infraestructura (Checkmarx KICS): reporte, consulta y descargas."""
import pandas as pd
import streamlit as st

from ejecucion import kics
from motores import MOTORES
from nucleo import config
from nucleo.errores import ErrorOperacion

from . import navegacion
from .comun import fecha

GRAVES = ("CRITICAL", "HIGH")


# ---------- piezas compartidas ----------
def _analizar(fuente: str, stack: str) -> int | None:
    etiqueta = kics.FUENTES[fuente]["etiqueta"]
    try:
        with st.spinner(f"KICS está analizando {etiqueta} · stack «{stack}» (suele tardar menos de un minuto)…"):
            esc_id = kics.escanear(fuente, stack, usuario=st.session_state.get("usuario", ""))
        st.toast(f"Análisis #{esc_id} terminado.", icon="🛡️")
        return esc_id
    except ErrorOperacion as e:
        st.error(f"{e.resumen}", icon="🚫")
        if e.detalle:
            with st.expander("Detalle técnico"):
                st.code(e.detalle)
    except Exception as e:  # noqa: BLE001 - se muestra a la persona, no se oculta
        st.error(f"No se pudo completar el análisis: {e}", icon="🚫")
    return None


def aviso_escaner(clave: str) -> bool:
    """Muestra por qué no se puede analizar (y permite instalar KICS). Devuelve True si está listo."""
    estado = kics.estado_escaner()
    if estado["listo"]:
        return True
    st.warning(estado["mensaje"], icon="⚠️")
    if estado["docker"] and not estado["imagen"]:
        if st.button("Instalar KICS (descargar la imagen)", key=f"instalar_{clave}"):
            try:
                with st.spinner("Descargando la imagen de KICS…"):
                    kics.instalar()
                st.rerun()
            except ErrorOperacion as e:
                st.error(f"{e.resumen}. {e.detalle}", icon="🚫")
    return False


def metricas(esc: dict):
    cols = st.columns(len(kics.SEVERIDADES) + 1)
    cols[0].metric("Hallazgos", esc["total"])
    for col, sev in zip(cols[1:], kics.SEVERIDADES):
        col.metric(f"{kics.ICONO[sev]} {kics.SEVERIDAD_ES[sev]}", esc["severidades"].get(sev, 0))


def tabla(hallazgos: list[dict], alto: int | None = None):
    df = pd.DataFrame([{
        "Severidad": f"{kics.ICONO.get(h['severidad'], '')} {kics.SEVERIDAD_ES.get(h['severidad'], h['severidad'])}",
        "Regla": h["regla"], "Categoría": h["categoria"], "Archivo": h["archivo"], "Línea": h["linea"],
        "Recurso": f"{h['recurso_tipo']}.{h['recurso']}".strip("."),
        "Valor actual": h["actual"], "Valor esperado": h["esperado"],
    } for h in hallazgos])
    st.dataframe(df, hide_index=True, width="stretch", **({"height": alto} if alto else {}))


def descargas(esc: dict, clave: str):
    """Botones para descargar el reporte del análisis en cada formato."""
    formatos = [f for f in kics.REPORTES if kics.reporte(esc["id"], f)]
    if not formatos:
        st.caption("Los archivos de este análisis ya no están disponibles.")
        return
    nombre = f"kics-{esc['fuente']}-{esc['stack']}-{esc['id']}"
    for col, formato in zip(st.columns(len(formatos)), formatos):
        etiqueta, archivo, mime = kics.REPORTES[formato]
        col.download_button(f"⬇️ {etiqueta}", kics.reporte(esc["id"], formato).read_bytes(),
                            file_name=f"{nombre}.{archivo.rsplit('.', 1)[1]}", mime=mime,
                            key=f"desc_{clave}_{esc['id']}_{formato}", width="stretch")


def _ficha(esc: dict) -> str:
    return (f"Análisis **#{esc['id']}** · {fecha(esc['creado'])} · KICS {esc['version_kics']} · "
            f"{esc['archivos']} archivo(s), {esc['lineas']} líneas, {esc['reglas']} reglas evaluadas · "
            f"{esc['duracion_s']:.0f} s")


def nota_pulumi():
    st.caption("ℹ️ KICS solo analiza Pulumi en formato YAML y trae pocas reglas para Pulumi (unas decenas, frente a "
               "más de mil para Terraform). La plataforma ejecuta el programa Python con el motor simulado de "
               "Pulumi (sin tocar AWS) y analiza el `Pulumi.yaml` resultante. Menos hallazgos aquí no significa "
               "más seguridad: el programa crea la misma infraestructura que las plantillas HCL, así que el "
               "reporte de Terraform / OpenTofu también aplica.")


# ---------- panel de la creación de tenants ----------
def panel_motor(motor: str) -> dict | None:
    """Reporte de seguridad del código que usará el motor elegido. Devuelve el análisis mostrado."""
    fuente = kics.fuente_de(motor)
    etiqueta_motor = MOTORES[motor].etiqueta
    with st.container(border=True):
        izq, der = st.columns([4, 1.4])
        izq.markdown(f"**🛡️ Seguridad del código de infraestructura · KICS** — motor **{etiqueta_motor}**")
        izq.caption(f"Código analizado: `{kics.FUENTES[fuente]['plantillas'] / 'tenant'}`"
                    + (" (los mismos archivos para Terraform y OpenTofu)" if fuente == "hcl" else ""))
        listo = aviso_escaner(f"panel_{fuente}")
        esc = kics.ultimo(fuente, "tenant")
        if listo and der.button("Analizar ahora" if not esc else "Volver a analizar", key=f"analizar_{fuente}",
                                type="primary" if not esc else "secondary", width="stretch"):
            if _analizar(fuente, "tenant"):
                st.rerun()
        if not esc:
            st.info("Todavía no hay un análisis de seguridad de este código. Ejecútelo antes de crear el tenant "
                    "para conocer los riesgos de la infraestructura que se va a desplegar.", icon="🛡️")
            return None

        st.caption(_ficha(esc))
        if kics.desactualizado(esc):
            st.warning("El código cambió después de este análisis: vuelva a analizar para ver el estado actual.",
                       icon="⚠️")
        metricas(esc)
        hallazgos = kics.hallazgos(esc["id"])
        graves = [h for h in hallazgos if h["severidad"] in GRAVES]
        if graves:
            st.error(f"Hay **{len(graves)}** hallazgo(s) de severidad alta o crítica. Revíselos antes de crear el "
                     "tenant; la creación no se bloquea.", icon="🚨")
        elif hallazgos:
            st.success("Sin hallazgos de severidad alta o crítica.", icon="✅")
        else:
            st.success("KICS no encontró hallazgos en este código.", icon="✅")
        if fuente == "pulumi":
            nota_pulumi()
        if hallazgos:
            relevantes = [h for h in hallazgos if h["severidad"] in (*GRAVES, "MEDIUM")]
            with st.expander(f"Ver hallazgos ({len(hallazgos)})", expanded=bool(graves)):
                solo = st.toggle("Solo severidad media, alta y crítica", value=bool(relevantes),
                                 key=f"solo_{fuente}", disabled=not relevantes)
                tabla(relevantes if solo and relevantes else hallazgos, alto=280)
        st.markdown("**Descargar el reporte**")
        descargas(esc, f"panel_{fuente}")
        if st.button("Consultar resultados e historial de análisis →", key=f"ir_{fuente}"):
            st.session_state["analisis_sel"] = esc["id"]
            st.switch_page(navegacion.PAGINAS["seguridad"])
        return esc


# ---------- página de consulta ----------
def pagina():
    st.title("🛡️ Seguridad del código de infraestructura")
    st.caption("Análisis estático con **Checkmarx KICS** de las plantillas de Terraform / OpenTofu y del programa "
               "de Pulumi. Analiza el código, no la nube: no usa credenciales de AWS ni modifica nada.")
    listo = aviso_escaner("pagina")

    # ---------- nuevo análisis ----------
    with st.container(border=True):
        st.markdown("**Nuevo análisis**")
        a, b, c = st.columns([2, 1.5, 1])
        fuente = a.selectbox("Código a analizar", list(kics.FUENTES), format_func=lambda f: kics.FUENTES[f]["etiqueta"])
        stack = b.selectbox("Stack", kics.stacks(fuente), help="tenant = infraestructura de cada tenant; monitoreo = "
                            "Grafana y Prometheus central; estado = bucket S3 del estado.")
        c.markdown("<div style='height:1.75rem'></div>", unsafe_allow_html=True)
        if c.button("Analizar con KICS", type="primary", disabled=not listo, width="stretch"):
            nuevo = _analizar(fuente, stack)
            if nuevo:
                st.session_state["analisis_sel"] = nuevo
                st.rerun()
        motores = ", ".join(MOTORES[m].etiqueta for m in kics.FUENTES[fuente]["motores"])
        st.caption(f"Motores que usan este código: {motores} · imagen `{config.IMAGEN_KICS}`")
        if fuente == "pulumi":
            nota_pulumi()

    # ---------- historial ----------
    st.subheader("Historial de análisis")
    todos = kics.listar()
    if not todos:
        st.info("Todavía no se ha ejecutado ningún análisis.")
        return
    filas = [{
        "N.º": e["id"], "Fecha": fecha(e["creado"]), "Código": kics.FUENTES.get(e["fuente"], {}).get("etiqueta", e["fuente"]),
        "Stack": e["stack"], "Resultado": {"exitoso": "✅ Terminado", "fallido": "❌ Falló",
                                           "en_progreso": "🔄 En curso"}.get(e["estado"], e["estado"]),
        "Hallazgos": e["total"], **{kics.SEVERIDAD_ES[s]: e["severidades"].get(s, 0) for s in kics.SEVERIDADES},
        "Vigente": "—" if e["estado"] != "exitoso" else ("No, el código cambió" if kics.desactualizado(e) else "Sí"),
        "Usuario": e["usuario"],
    } for e in todos]
    evento = st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch", on_select="rerun",
                          selection_mode="single-row", key="tabla_analisis")
    st.caption(f"{len(filas)} análisis (se conservan los {config.ESCANEOS_CONSERVADOS} más recientes de cada código "
               "y stack). Seleccione una fila para consultar sus resultados.")
    if evento.selection.rows:
        st.session_state["analisis_sel"] = filas[evento.selection.rows[0]]["N.º"]
    ids = [e["id"] for e in todos]
    sel = st.session_state.get("analisis_sel")
    esc = kics.obtener(sel if sel in ids else ids[0])

    # ---------- resultados del análisis elegido ----------
    st.divider()
    st.subheader(f"Resultados del análisis #{esc['id']} · {kics.FUENTES[esc['fuente']]['etiqueta']} · {esc['stack']}")
    if esc["estado"] != "exitoso":
        st.error("Este análisis no terminó." if esc["estado"] == "en_progreso" else "Este análisis falló.", icon="🚫")
        if esc["error"]:
            st.code(esc["error"])
        return
    st.caption(_ficha(esc))
    if kics.desactualizado(esc):
        st.warning("El código cambió después de este análisis.", icon="⚠️")
    metricas(esc)
    st.markdown("**Descargar el reporte**")
    descargas(esc, "pagina")

    hallazgos = kics.hallazgos(esc["id"])
    if not hallazgos:
        st.success("KICS no encontró hallazgos en este código.", icon="✅")
        return
    a, b, c = st.columns([2, 2, 2])
    presentes = [s for s in kics.SEVERIDADES if any(h["severidad"] == s for h in hallazgos)]
    sev = a.multiselect("Severidad", presentes, format_func=lambda s: f"{kics.ICONO[s]} {kics.SEVERIDAD_ES[s]}",
                        key=f"f_sev_{esc['id']}")
    cat = b.multiselect("Categoría", sorted({h["categoria"] for h in hallazgos}), key=f"f_cat_{esc['id']}")
    texto = c.text_input("Buscar (regla, archivo o recurso)", key=f"f_txt_{esc['id']}").strip().lower()
    filtrados = [h for h in hallazgos
                 if (not sev or h["severidad"] in sev) and (not cat or h["categoria"] in cat)
                 and (not texto or texto in f"{h['regla']} {h['archivo']} {h['recurso_tipo']} {h['recurso']}".lower())]
    st.caption(f"{len(filtrados)} de {len(hallazgos)} hallazgo(s).")
    if not filtrados:
        st.info("Ningún hallazgo coincide con los filtros.")
        return
    tabla(filtrados)

    st.markdown("**Detalle por regla**")
    reglas: dict[str, list[dict]] = {}
    for h in filtrados:
        reglas.setdefault(h["regla_id"] or h["regla"], []).append(h)
    for grupo in reglas.values():
        h0 = grupo[0]
        with st.expander(f"{kics.ICONO.get(h0['severidad'], '')} {kics.SEVERIDAD_ES.get(h0['severidad'])} · "
                         f"{h0['regla']} ({len(grupo)})"):
            st.write(h0["descripcion"])
            st.caption(" · ".join(x for x in (
                f"Categoría: {h0['categoria']}", f"CWE-{h0['cwe']}" if h0["cwe"] else "",
                f"[Documentación del recurso]({h0['url']})" if h0["url"] else "") if x))
            for h in grupo:
                st.markdown(f"`{h['archivo']}:{h['linea']}` — `{h['recurso_tipo']}.{h['recurso']}`  \n"
                            f"Actual: {h['actual']}  \nEsperado: {h['esperado']}")
                codigo = kics.fragmento(esc["id"], h["archivo"], h["linea"])
                if codigo:
                    st.code(codigo, language=None)

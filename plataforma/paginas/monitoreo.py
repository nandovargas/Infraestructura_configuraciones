import pandas as pd
import streamlit as st

from nucleo import config, db, monitoreo, servicios

from .comun import fecha

ROLES = ["frontend", "backend", "bd"]


def _por(res, clave, labels=("tenant", "entorno")):
    if isinstance(res.get(clave), Exception):
        return {}
    return {tuple(m.get(l, "") for l in labels): v for m, v in res.get(clave, [])}


def tabla_metricas(url, filtro, central=True):
    """Tabla de salud por tenant con los valores actuales (disponibilidad, CPU, memoria, latencia, errores, BD)."""
    if not url:
        st.info("No hay una fuente de métricas disponible.")
        return
    try:
        res = monitoreo.instantaneas(url, filtro)
    except Exception as e:
        st.error(f"No se pudo consultar Prometheus en {url}: {e}")
        return
    errores = [k for k, v in res.items() if isinstance(v, Exception)]
    if len(errores) == len(res):
        st.error(f"Prometheus no respondió ({url}). ¿Está activo el tenant/monitoreo y su IP autorizada?")
        return
    disp, lat, req, err, bd, con = (_por(res, k) for k in
                                    ("disponibilidad", "latencia_p95", "peticiones", "errores_pct", "bd_up",
                                     "bd_conexiones"))
    cpu, mem, disco = (_por(res, k, ("tenant", "entorno", "rol")) for k in ("cpu", "memoria", "disco"))
    caidos = {}
    for m, v in res.get("servicios", []) if not isinstance(res.get("servicios"), Exception) else []:
        clave = (m.get("tenant", ""), m.get("entorno", ""))
        caidos.setdefault(clave, [])
        if v == 0:
            caidos[clave].append(f"{m.get('job')}/{m.get('rol')}")
    claves = sorted(set(disp) | set(caidos) | set(bd))
    if not claves:
        st.info("Todavía no hay métricas (los datos aparecen 1-2 minutos después del despliegue).")
        return
    filas = []
    for k in claves:
        fila = {"Tenant": k[0] or "(este tenant)", "Entorno": k[1] or "—",
                "Disponibilidad 1 h": disp.get(k),
                "Servicios caídos": ", ".join(caidos.get(k, [])) or "ninguno",
                "Latencia p95 (ms)": round(lat[k] * 1000, 1) if lat.get(k) == lat.get(k) and k in lat else None,
                "Peticiones/s": round(req.get(k, 0), 2),
                "Errores 5xx %": round(err.get(k, 0), 2),
                "Base de datos": {1.0: "🟢 OK", 0.0: "🔴 caída"}.get(bd.get(k), "—"),
                "Conexiones BD": con.get(k)}
        for rol in ROLES:
            fila[f"CPU {rol} %"] = round(cpu[(*k, rol)], 1) if (*k, rol) in cpu else None
            fila[f"Mem {rol} %"] = round(mem[(*k, rol)], 1) if (*k, rol) in mem else None
        fila["Disco BD %"] = round(disco[(*k, "bd")], 1) if (*k, "bd") in disco else None
        filas.append(fila)
    barra = lambda t: st.column_config.ProgressColumn(t, min_value=0, max_value=100, format="%.1f")  # noqa: E731
    st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch", column_config={
        "Disponibilidad 1 h": barra("Disponibilidad 1 h"),
        **{f"CPU {r} %": barra(f"CPU {r} %") for r in ROLES},
        **{f"Mem {r} %": barra(f"Mem {r} %") for r in ROLES},
        "Disco BD %": barra("Disco BD %"),
    })
    if errores:
        st.caption(f"Sin datos para: {', '.join(errores)}")


def _grafico(url, expr, titulo, horas, leyenda):
    try:
        series = monitoreo.rango(url, expr, horas, paso=max(60, horas * 30))
    except Exception as e:
        st.caption(f"{titulo}: sin datos ({e})")
        return
    if not series:
        st.caption(f"{titulo}: sin datos")
        return
    df = pd.DataFrame()
    for s in series:
        nombre = " · ".join(s["metric"].get(l, "") for l in leyenda if s["metric"].get(l))
        valores = pd.Series({pd.to_datetime(t, unit="s", utc=True).tz_convert(None): float(v) for t, v in s["values"]})
        df[nombre or "valor"] = valores
    st.markdown(f"**{titulo}**")
    st.line_chart(df, height=220)


def pagina():
    st.title("📈 Salud, métricas y alertas")
    m = db.get_config("monitoreo")
    if not m:
        st.warning("La instancia de monitoreo (Grafana + Prometheus central) no está desplegada. "
                   "Despliéguela en «Infraestructura» para ver métricas de todos los tenants y recibir alertas.")
        return

    a, b, c = st.columns([2, 2, 1])
    entornos = a.multiselect("Entorno", config.ENTORNOS, default=config.ENTORNOS)
    activos = [t for t in servicios.listar_tenants(entornos=entornos) if t["estado"] in ("activo", "error")]
    tenant = b.selectbox("Tenant", ["(todos)"] + [t["id"] for t in activos])
    horas = c.selectbox("Periodo de los gráficos", [1, 3, 6, 12, 24], index=1, format_func=lambda h: f"{h} h")
    tenant = None if tenant == "(todos)" else tenant
    f = monitoreo.filtro(tenant, entornos)

    var_t = f"&var-tenant={tenant}" if tenant else ""
    var_e = "".join(f"&var-entorno={e}" for e in entornos)
    st.link_button("📊 Abrir en Grafana", f"{m['grafana_url']}/d/asistencia-tenants?from=now-{horas}h{var_t}{var_e}")

    st.subheader("Estado actual por tenant")
    tabla_metricas(m["prometheus_url"], f)

    st.subheader("🚨 Alertas y eventos recientes")
    solo = st.toggle("Solo alertas activas", value=False)
    ev = servicios.listar_eventos(tenant_id=tenant, entornos=entornos if not tenant else None,
                                  solo_alertas=True, solo_activas=solo, limite=100)
    if ev:
        st.dataframe(pd.DataFrame([{
            "Detectada": fecha(e["detectado_en"]), "Tenant afectado": e["tenant_id"] or "—",
            "Entorno": e["entorno"] or "—", "Severidad": {"critical": "🔴 crítica", "warning": "🟠 advertencia"}
            .get(e["severidad"], e["severidad"]), "Alerta": e["titulo"], "Detalle": e["detalle"],
            "Estado": "🔴 activa" if e["estado"] == "activa" else "✅ resuelta", "Resuelta": fecha(e["resuelto_en"]),
        } for e in ev]), hide_index=True, width="stretch")
    else:
        st.success("Sin alertas en el periodo registrado.")
    st.caption("Las alertas se evalúan en el Prometheus central cada 30 s y la plataforma las sincroniza cada minuto. "
               "Reglas: servicio o tenant sin respuesta, base de datos caída, errores 5xx > 5 %, latencia p95 > 1 s, "
               "CPU > 85 %, memoria > 90 %, disco > 85 %, conexiones BD > 80 %.")

    st.subheader("Tendencias")
    url = m["prometheus_url"]
    lat = f"rate(django_http_requests_latency_seconds_by_view_method_bucket{{{f.lstrip(', ')}}}[5m])"
    g1, g2 = st.columns(2)
    with g1:
        _grafico(url, f'100 * (1 - avg by (tenant, rol) (rate(node_cpu_seconds_total{{mode="idle"{f}}}[5m])))',
                 "CPU (%)", horas, ("tenant", "rol"))
        _grafico(url, f"histogram_quantile(0.95, sum by (tenant, le) ({lat}))", "Latencia p95 (s)", horas,
                 ("tenant",))
    with g2:
        _grafico(url, f'100 * (1 - avg by (tenant, rol) (node_memory_MemAvailable_bytes{{rol!=""{f}}} '
                      f'/ node_memory_MemTotal_bytes{{rol!=""{f}}}))', "Memoria (%)", horas, ("tenant", "rol"))
        _grafico(url, f'sum by (tenant) (rate(django_http_responses_total_by_status_total{{status=~"5.."{f}}}[5m]))',
                 "Errores 5xx por segundo", horas, ("tenant",))

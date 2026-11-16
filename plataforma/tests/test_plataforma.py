import json

import pytest

from ejecucion import kics
from motores import MOTORES, obtener, para_tenants
from motores.pulumi import MotorPulumi, direcciones
from motores.terraform import MotorTerraform
from nucleo import config, db, errores, servicios
from nucleo.seguridad import MASCARA, Redactor, crear_admin, verificar_admin

TENANT = dict(id="colegio-andino", nombre="Colegio Andino", contacto_nombre="Ana", contacto_email="ana@andino.edu",
              entorno="dev", plan_id="basico", motor="terraform")


@pytest.fixture(autouse=True)
def motores_instalados(monkeypatch):
    """Las pruebas no dependen de que las CLI estén instaladas en la máquina."""
    for clase in MOTORES.values():
        monkeypatch.setattr(clase, "instalado", classmethod(lambda cls: True))


# ---------- Planes ----------
def test_planes_iniciales_y_costos():
    planes = {p["id"]: p for p in servicios.listar_planes()}
    assert set(planes) == {"basico", "estandar", "avanzado"}
    costos = [servicios.estimar_costo(planes[i]) for i in ("basico", "estandar", "avanzado")]
    assert costos == sorted(costos) and costos[0] > 0
    assert servicios.costo_suspendido(planes["basico"]) < costos[0]


def test_validacion_de_planes():
    malo = dict(id="X", nombre="", tipo_frontend="m5.24xlarge", tipo_backend="t3.micro", tipo_bd="t3.micro",
                disco_app_gb=8, disco_bd_gb=20, max_estudiantes=-1, gunicorn_threads=0, retencion_dias=0, activo=1)
    errs = servicios.validar_plan(malo, nuevo=True)
    assert len(errs) >= 6


# ---------- Tenants y cola ----------
def test_crear_tenant_encola_despliegue_y_reserva_id():
    op = servicios.crear_tenant(dict(TENANT))
    t = servicios.obtener_tenant("colegio-andino")
    assert t["estado"] == "aprovisionando"
    assert t["backend_estado"]["tipo"] == "local"
    assert servicios.obtener_operacion(op)["tipo"] == "desplegar"
    with pytest.raises(servicios.ErrorValidacion, match="ya está en uso"):
        servicios.crear_tenant(dict(TENANT))


@pytest.mark.parametrize("campo,valor", [("id", "Colegio!"), ("contacto_email", "no-es-correo"),
                                         ("entorno", "qa"), ("motor", "cloudformation"), ("plan_id", "inexistente")])
def test_validacion_de_tenant(campo, valor):
    with pytest.raises(servicios.ErrorValidacion):
        servicios.crear_tenant(dict(TENANT, **{campo: valor}))


def test_una_sola_operacion_activa_por_tenant():
    servicios.crear_tenant(dict(TENANT))
    with pytest.raises(servicios.ErrorValidacion, match="operación en curso"):
        servicios.encolar("tenant", "desplegar", "colegio-andino")
    # sin acciones permitidas mientras hay una operación activa
    assert servicios.acciones_permitidas(servicios.obtener_tenant("colegio-andino")) == set()


def _terminar(op_id, estado, tenant_estado):
    db.ejecutar("UPDATE operaciones SET estado = ? WHERE id = ?", (estado, op_id))
    db.ejecutar("UPDATE tenants SET estado = ? WHERE id = 'colegio-andino'", (tenant_estado,))


def test_reintento_solo_de_la_ultima_fallida_y_cuenta_intentos():
    op1 = servicios.crear_tenant(dict(TENANT))
    _terminar(op1, "fallida", "error")
    op2 = servicios.reintentar(op1)
    assert servicios.obtener_operacion(op2)["intento"] == 2
    assert servicios.obtener_operacion(op2)["reintento_de"] == op1
    _terminar(op2, "fallida", "error")
    with pytest.raises(servicios.ErrorValidacion, match="más reciente"):
        servicios.reintentar(op1)


def test_ciclo_de_vida_valida_transiciones():
    op = servicios.crear_tenant(dict(TENANT))
    _terminar(op, "exitosa", "activo")
    with pytest.raises(servicios.ErrorValidacion):
        servicios.solicitar("colegio-andino", "reanudar")
    op = servicios.solicitar("colegio-andino", "suspender")
    assert servicios.obtener_tenant("colegio-andino")["estado"] == "suspendiendo"
    _terminar(op, "exitosa", "suspendido")
    assert servicios.acciones_permitidas(servicios.obtener_tenant("colegio-andino")) == {"reanudar", "eliminar"}


def test_no_se_reducen_discos_al_cambiar_de_plan():
    db.ejecutar("UPDATE planes SET disco_bd_gb = 200 WHERE id = 'basico'")
    op = servicios.crear_tenant(dict(TENANT))
    _terminar(op, "exitosa", "activo")
    with pytest.raises(servicios.ErrorValidacion, match="discos más pequeños"):
        servicios.solicitar("colegio-andino", "desplegar", plan_id="estandar")


def test_filtros_de_consulta():
    servicios.crear_tenant(dict(TENANT))
    servicios.crear_tenant(dict(TENANT, id="uni-sur", nombre="Universidad del Sur", entorno="prod",
                                plan_id="avanzado"))
    assert [t["id"] for t in servicios.listar_tenants(texto="sur")] == ["uni-sur"]
    assert [t["id"] for t in servicios.listar_tenants(entornos=["dev"])] == ["colegio-andino"]
    assert [t["id"] for t in servicios.listar_tenants(planes=["avanzado"])] == ["uni-sur"]
    assert len(servicios.listar_tenants(estados=["aprovisionando"])) == 2


# ---------- Secretos ----------
def test_redactor_oculta_valores_y_patrones():
    r = Redactor()
    r.agregar({"db_password": "SuperSecreta123", "ssh": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----"})
    texto = ("conexión con SuperSecreta123 · password=otra-clave · AKIAABCDEFGHIJKLMNOP · "
             "postgres://u:pw123@h/db · -----BEGIN OPENSSH PRIVATE KEY-----\nxyz\n-----END OPENSSH PRIVATE KEY-----")
    limpio = r.limpiar(texto)
    for secreto in ("SuperSecreta123", "otra-clave", "AKIAABCDEFGHIJKLMNOP", "pw123", "xyz"):
        assert secreto not in limpio
    assert MASCARA in limpio


def test_admin_con_hash():
    crear_admin("admin", "una-clave-larga")
    assert verificar_admin("admin", "una-clave-larga")
    assert not verificar_admin("admin", "otra")


# ---------- Errores comprensibles ----------
@pytest.mark.parametrize("tecnico,esperado", [
    ("Error: InvalidClientTokenId: The security token included in the request is invalid", "credenciales de AWS"),
    ("Error: Your query returned no results. Please change your search criteria", "AMIs de Packer"),
    ("error during connect: open //./pipe/dockerDesktopLinuxEngine", "Docker"),
    ("fatal: [x]: UNREACHABLE! => {}", "SSH"),
    ("Error acquiring the state lock", "bloqueado"),
])
def test_traduccion_de_errores(tecnico, esperado):
    mensaje, sugerencia = errores.traducir(tecnico)
    assert esperado.lower() in mensaje.lower() and sugerencia


# ---------- Alertas ----------
def test_alertas_se_registran_y_resuelven_salvo_en_mantenimiento(monkeypatch):
    from nucleo import monitoreo
    db.set_config("monitoreo", {"prometheus_url": "http://x"})
    for tid, estado in (("t-activo", "activo"), ("t-suspendido", "suspendiendo")):
        servicios.crear_tenant(dict(TENANT, id=tid))
        db.ejecutar("UPDATE tenants SET estado = ? WHERE id = ?", (estado, tid))

    def alerta(tid):
        return {"state": "firing", "activeAt": "2026-01-01T10:00:00.123Z", "annotations": {"summary": "caído"},
                "labels": {"alertname": "ServicioCaido", "tenant": tid, "entorno": "dev", "severity": "critical"}}

    respuesta = {"alerts": [alerta("t-activo"), alerta("t-suspendido")]}
    monkeypatch.setattr(monitoreo, "_get", lambda *a: respuesta)
    assert monitoreo.sincronizar_alertas() == (1, 0)
    assert monitoreo.sincronizar_alertas() == (0, 0)          # no duplica mientras sigue activa
    ev = servicios.listar_eventos(solo_alertas=True)
    assert [(e["tenant_id"], e["estado"], e["detectado_en"]) for e in ev] == \
        [("t-activo", "activa", "2026-01-01T10:00:00+00:00")]
    respuesta["alerts"] = []
    assert monitoreo.sincronizar_alertas() == (0, 1)
    assert servicios.listar_eventos(solo_alertas=True)[0]["resuelto_en"]


# ---------- Ansible ----------
def test_variables_del_tenant_van_como_extra_vars():
    """Si fueran [all:vars], los group_vars del playbook (admin1234, tenant por defecto) las pisarían."""
    from ejecucion.ansible import inventario_ini
    inv = inventario_ini({"backend": [("b", {"ansible_host": "1.2.3.4"})]},
                         {"tenant_id": "x", "admin_password": "generada", "ansible_user": "ubuntu"})
    assert "[all:vars]" not in inv["ini"] and "generada" not in inv["ini"]
    assert inv["vars"]["admin_password"] == "generada"
    # las de conexión van por host (no deben aplicarse a localhost)
    assert "b ansible_host=1.2.3.4 ansible_user=ubuntu" in inv["ini"] and "ansible_user" not in inv["vars"]


# ---------- Motor ----------
def test_registro_de_motores():
    for m in ("terraform", "opentofu", "pulumi"):
        assert MOTORES[m].disponible and MOTORES[m].soporta("tenant")
    assert set(para_tenants()) == {"terraform", "opentofu", "pulumi"}
    assert not MOTORES["cloudformation"].disponible
    with pytest.raises(ValueError):
        obtener("cloudformation", plantilla=None, trabajo=None, backend={}, log=print)
    # Pulumi solo implementa el stack del tenant; monitoreo y estado siguen en Terraform
    with pytest.raises(ValueError, match="no implementa"):
        obtener("pulumi", stack="monitoreo", trabajo=None, backend={}, log=print)


def test_plantilla_segun_motor(tmp_path):
    b = {"tipo": "local", "path": str(tmp_path / "tenants" / "x.tfstate")}
    tf = obtener("terraform", stack="tenant", trabajo=tmp_path, backend=b, log=print)
    tofu = obtener("opentofu", stack="tenant", trabajo=tmp_path, backend=b, log=print)
    pu = obtener("pulumi", stack="tenant", trabajo=tmp_path, backend=b, log=print)
    assert tf.plantilla == tofu.plantilla == config.PLANTILLAS_TF / "tenant"
    assert pu.plantilla == config.PLANTILLAS_PULUMI / "tenant"
    assert (pu.plantilla / "__main__.py").exists() and (pu.plantilla / "Pulumi.yaml").exists()
    assert tofu.binario == "tofu"


def test_crear_tenant_con_cada_motor():
    for motor in ("opentofu", "pulumi"):
        servicios.crear_tenant(dict(TENANT, id=f"colegio-{motor}", motor=motor))
        assert servicios.obtener_tenant(f"colegio-{motor}")["motor"] == motor


def test_motor_no_instalado_se_rechaza(monkeypatch):
    monkeypatch.setattr(MOTORES["opentofu"], "instalado", classmethod(lambda cls: False))
    with pytest.raises(servicios.ErrorValidacion, match="no está instalado"):
        servicios.crear_tenant(dict(TENANT, motor="opentofu"))


def test_pulumi_backend_y_stack(tmp_path):
    local = MotorPulumi(tmp_path, tmp_path, {"tipo": "local", "path": str(tmp_path / "tenants" / "upsa.tfstate")},
                        log=print)
    assert local.stack == "upsa"
    assert local.url_backend().startswith("file://") and local.url_backend().endswith("/tenants/upsa.pulumi")
    s3 = MotorPulumi(tmp_path, tmp_path, {"tipo": "s3", "bucket": "b", "key": "tenants/upsa/terraform.tfstate",
                                          "region": "us-east-1"}, log=print)
    assert s3.stack == "upsa"
    assert s3.url_backend() == "s3://b/pulumi/tenants/upsa?region=us-east-1&awssdk=v2"


def test_pulumi_progreso_y_resumen(tmp_path):
    lineas, avances = [], []
    m = MotorPulumi(tmp_path, tmp_path, {"tipo": "local", "path": str(tmp_path / "x.tfstate")}, log=lineas.append)
    estado = {"total": 4, "hechos": 0, "resumen": {}}
    manejar = m._linea_progreso(lambda f, msg: avances.append(f), estado)
    salida = [" +  pulumi:pulumi:Stack asistencia-tenant-upsa creating (0s)",
              " +  aws:ec2:SecurityGroup frontend creating (0s)",
              "@ updating....",
              " +  aws:ec2:SecurityGroup frontend created (2s)",
              " +  aws:ec2:Instance bd created (35s)",
              "Resources:", "    + 30 created", "    2 unchanged",
              "    error: creating EC2 Instance: VcpuLimitExceeded"]
    errs = [manejar(l) for l in salida]
    assert avances == [0.25, 0.5]
    assert estado["resumen"] == {"add": 30, "unchanged": 2}
    assert errs[-1].startswith("error:") and "@ updating...." not in lineas


def test_pulumi_direcciones_estilo_terraform():
    exportado = {"deployment": {"resources": [
        {"urn": "urn:pulumi:upsa::asistencia-tenant::pulumi:pulumi:Stack::asistencia-tenant-upsa",
         "type": "pulumi:pulumi:Stack"},
        {"urn": "urn:pulumi:upsa::asistencia-tenant::pulumi:providers:aws::aws", "type": "pulumi:providers:aws"},
        {"urn": "urn:pulumi:upsa::asistencia-tenant::aws:ec2/instance:Instance::frontend",
         "type": "aws:ec2/instance:Instance"},
        {"urn": "urn:pulumi:upsa::asistencia-tenant::random:index/randomPassword:RandomPassword::db",
         "type": "random:index/randomPassword:RandomPassword"},
    ]}}
    assert direcciones(exportado) == ["aws_instance.frontend", "random_password.db"]


def test_backend_s3_cifrado_con_bloqueo(tmp_path):
    m = MotorTerraform(tmp_path, tmp_path, {"tipo": "s3", "bucket": "b", "key": "tenants/x/terraform.tfstate",
                                            "region": "us-east-1"}, log=print)
    hcl = m._backend_hcl()
    assert 'backend "s3"' in hcl and "encrypt      = true" in hcl and "use_lockfile = true" in hcl


def test_progreso_desde_salida_json(tmp_path):
    lineas, avances = [], []
    m = MotorTerraform(tmp_path, tmp_path, {"tipo": "local", "path": str(tmp_path / "s")}, log=lineas.append)
    estado = {"total": 0, "hechos": 0, "resumen": {}}
    manejar = m._linea_json(lambda f, msg: avances.append(f), estado)
    eventos = [{"type": "planned_change", "@message": "a: Plan to create"},
               {"type": "planned_change", "@message": "b: Plan to create"},
               {"type": "apply_complete", "@message": "a: Creation complete"},
               {"type": "apply_complete", "@message": "b: Creation complete"},
               {"type": "diagnostic", "@message": "x", "diagnostic": {"severity": "error", "summary": "Falló",
                                                                       "detail": "detalle"}}]
    errs = [manejar(json.dumps(e)) for e in eventos]
    assert avances == [0.5, 1.0]
    assert errs[-1].startswith("ERROR: Falló")


# ---------- URL del tenant ----------
def test_url_del_tenant_usa_el_identificador_como_subdominio(monkeypatch):
    from nucleo import config
    monkeypatch.setattr(config, "DOMINIO_TENANTS", "")
    assert config.url_tenant("upsa", "54.172.11.111") + "/panel/login" == "http://upsa.54.172.11.111/panel/login"
    monkeypatch.setattr(config, "DOMINIO_TENANTS", "nip.io")
    assert config.url_tenant("uagrm", "44.215.224.136") == "http://uagrm.44.215.224.136.nip.io"


# ---------- Análisis de seguridad (KICS) ----------
RESULTADO_KICS = {
    "kics_version": "v2.2.1", "files_scanned": 4, "lines_scanned": 482, "queries_total": 1101, "total_counter": 3,
    "severity_counters": {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 0, "LOW": 0, "INFO": 2, "TRACE": 0},
    "queries": [
        {"query_name": "Variable Without Description", "query_id": "q-info", "severity": "INFO",
         "category": "Best Practices", "platform": "Terraform", "description": "d", "query_url": "u", "cwe": "",
         "files": [{"file_name": "../../src/variables.tf", "line": 28, "resource_type": "", "resource_name": "x",
                    "expected_value": "e", "actual_value": "a"},
                   {"file_name": "../../src/variables.tf", "line": 5, "resource_type": "", "resource_name": "y",
                    "expected_value": "e", "actual_value": "a"}]},
        {"query_name": "Unrestricted Security Group Ingress", "query_id": "q-high", "severity": "HIGH",
         "category": "Networking and Firewall", "platform": "Terraform", "description": "d", "query_url": "u",
         "cwe": "668", "files": [{"file_name": "../../src/main.tf", "line": 2,
                                  "resource_type": "aws_vpc_security_group_ingress_rule",
                                  "resource_name": "frontend_http", "expected_value": "e", "actual_value": "a"}]},
    ]}


def test_kics_fuente_por_motor():
    assert kics.fuente_de("terraform") == kics.fuente_de("opentofu") == "hcl"
    assert kics.fuente_de("pulumi") == "pulumi"
    assert kics.stacks("hcl")[0] == "tenant" and kics.stacks("pulumi") == ["tenant"]
    assert kics.FUENTES["hcl"]["tipo"] == "Terraform" and kics.FUENTES["pulumi"]["tipo"] == "Pulumi"


def test_kics_hallazgos_ordenados_por_severidad():
    filas = kics.aplanar(RESULTADO_KICS)
    assert [f["severidad"] for f in filas] == ["HIGH", "INFO", "INFO"]
    assert filas[0]["archivo"] == "main.tf" and filas[0]["recurso"] == "frontend_http"
    assert [f["linea"] for f in filas[1:]] == [5, 28]


def _escaneo_falso(fuente="hcl", huella=None):
    esc_id, _ = db.ejecutar(
        "INSERT INTO escaneos(fuente, stack, estado, huella, version_kics, severidades, total, creado)"
        " VALUES(?, 'tenant', 'exitoso', ?, 'v2.2.1', ?, 3, ?)",
        (fuente, huella or kics.huella_actual(fuente, "tenant"), json.dumps(RESULTADO_KICS["severity_counters"]),
         db.ahora()))
    carpeta = config.ESCANEOS / str(esc_id)
    (carpeta / "fuente").mkdir(parents=True, exist_ok=True)
    (carpeta / "resultados.json").write_text(json.dumps(RESULTADO_KICS), encoding="utf-8")
    (carpeta / "resultados.html").write_text("<html></html>", encoding="utf-8")
    (carpeta / "fuente" / "main.tf").write_text("linea 1\nlinea 2\nlinea 3\n", encoding="utf-8")
    return esc_id


def test_kics_consulta_y_reportes():
    viejo = _escaneo_falso(huella="otra-huella")
    nuevo = _escaneo_falso()
    assert kics.ultimo("hcl")["id"] == nuevo and kics.ultimo("pulumi") is None
    assert [e["id"] for e in kics.listar()] == [nuevo, viejo]
    assert kics.desactualizado(kics.obtener(viejo)) and not kics.desactualizado(kics.obtener(nuevo))
    assert kics.obtener(nuevo)["severidades"]["HIGH"] == 1
    assert len(kics.hallazgos(nuevo)) == 3
    assert kics.reporte(nuevo, "html").name == "resultados.html"
    assert kics.reporte(nuevo, "pdf") is None and kics.reporte(nuevo, "exe") is None


def test_kics_fragmento_no_sale_de_la_carpeta():
    esc = _escaneo_falso()
    assert ">    2 | linea 2" in kics.fragmento(esc, "main.tf", 2, contexto=1)
    assert kics.fragmento(esc, "../resultados.json", 1) == ""
    assert kics.fragmento(esc, "no-existe.tf", 1) == ""


def test_kics_purga_analisis_antiguos(monkeypatch):
    monkeypatch.setattr(config, "ESCANEOS_CONSERVADOS", 2)
    ids = [_escaneo_falso() for _ in range(4)]
    kics._purgar("hcl", "tenant")
    assert [e["id"] for e in kics.listar()] == ids[:1:-1]
    assert not (config.ESCANEOS / str(ids[0])).exists() and (config.ESCANEOS / str(ids[3])).exists()

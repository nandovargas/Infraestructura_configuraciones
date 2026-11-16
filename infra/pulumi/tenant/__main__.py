"""Stack de UN tenant con Pulumi (Python). Equivalente a infra/terraform/tenant.

Crea los mismos recursos, con los mismos nombres lógicos y las mismas salidas, para que la
plataforma (inventario de Ansible, snapshots, vista previa de eliminación) funcione igual
sin importar el motor elegido.

La plataforma copia esta carpeta a la copia de trabajo del tenant, junto con las plantillas
cloud-init de infra/terraform/tenant/cloud-init (una sola fuente para ambos motores), y
carga la configuración con `pulumi config set-all`.
"""
import re
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_random as random
import pulumi_tls as tls

cfg = pulumi.Config()

# ---------- Variables (mismas que variables.tf) ----------
region = cfg.get("region") or "us-east-1"
tenant_id = cfg.require("tenant_id")
entorno = cfg.require("entorno")
plan = cfg.require("plan")
tipo_frontend = cfg.require("tipo_frontend")
tipo_backend = cfg.require("tipo_backend")
tipo_bd = cfg.require("tipo_bd")
disco_app_gb = cfg.require_int("disco_app_gb")
disco_bd_gb = cfg.require_int("disco_bd_gb")
estado = cfg.get("estado") or "running"
admin_cidr = cfg.require("admin_cidr")
db_name = cfg.get("db_name") or "asistencia_db"
db_user = cfg.get("db_user") or "asistencia"
dominio_base = cfg.get("dominio_base")
dominio_base = "nip.io" if dominio_base is None else dominio_base
gestion = cfg.get("gestion") or "pulumi"

if not re.fullmatch(r"[a-z][a-z0-9-]{2,20}", tenant_id):
    raise ValueError("tenant_id: 3-21 caracteres, empieza con letra, solo a-z, 0-9 y guiones.")
if entorno not in ("dev", "staging", "prod"):
    raise ValueError("entorno debe ser dev, staging o prod.")
if estado not in ("running", "stopped"):
    raise ValueError("estado debe ser running o stopped.")

nombre = f"asist-{tenant_id}-{entorno}"

# Credenciales: el proveedor lee AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY del entorno.
proveedor = aws.Provider("aws", region=region, default_tags=aws.ProviderDefaultTagsArgs(tags={
    "Plataforma": "asistencia-saas", "Tenant": tenant_id, "Entorno": entorno, "Plan": plan, "Gestion": gestion,
}))
op = pulumi.ResourceOptions(provider=proveedor)
inv = pulumi.InvokeOptions(provider=proveedor)


def opciones(**kwargs) -> pulumi.ResourceOptions:
    return pulumi.ResourceOptions.merge(op, pulumi.ResourceOptions(**kwargs))


# ---------- Red: VPC y subredes por defecto ----------
vpc = aws.ec2.get_vpc(default=True, opts=inv)
subredes = aws.ec2.get_subnets(filters=[aws.ec2.GetSubnetsFilterArgs(name="vpc-id", values=[vpc.id])], opts=inv)
subnet_id = sorted(subredes.ids)[0]

# Grupo de seguridad de la instancia de monitoreo (si ya está desplegada)
sg_monitoreo = aws.ec2.get_security_groups(filters=[
    aws.ec2.GetSecurityGroupsFilterArgs(name="tag:Plataforma", values=["asistencia-saas"]),
    aws.ec2.GetSecurityGroupsFilterArgs(name="tag:Rol", values=["monitoreo"]),
], opts=inv)


# ---------- AMIs creadas con Packer ----------
def ami(nombre_ami: str) -> str:
    return aws.ec2.get_ami(owners=["self"], most_recent=True, opts=inv,
                           filters=[aws.ec2.GetAmiFilterArgs(name="name", values=[nombre_ami])]).id


# ---------- Secretos del tenant (solo viven en el estado cifrado) ----------
pass_db = random.RandomPassword("db", length=24, special=False)
django_secret = random.RandomPassword("django_secret", length=50, special=False)
admin_app = random.RandomPassword("admin_app", length=16, special=False)
clave_ansible = tls.PrivateKey("ansible", algorithm="ED25519")

par_claves = aws.ec2.KeyPair("ansible", key_name_prefix=f"{nombre}-",
                             public_key=clave_ansible.public_key_openssh, opts=op)


# ---------- Grupos de seguridad (uno por capa y por tenant) ----------
def grupo(capa: str, descripcion: str) -> aws.ec2.SecurityGroup:
    return aws.ec2.SecurityGroup(capa, name_prefix=f"{nombre}-{capa}-", description=descripcion,
                                 vpc_id=vpc.id, tags={"Name": f"{nombre}-{capa}", "Rol": capa},
                                 opts=opciones(delete_before_replace=False))


sg = {
    "frontend": grupo("frontend", f"Frontend {nombre}: HTTP publico"),
    "backend": grupo("backend", f"Backend {nombre}: API Django y Prometheus"),
    "bd": grupo("bd", f"BD {nombre}: PostgreSQL solo desde su backend"),
}


def entrada(nombre_regla, grupo_id, puerto, cidr=None, origen=None):
    return aws.vpc.SecurityGroupIngressRule(
        nombre_regla, security_group_id=grupo_id, ip_protocol="tcp", from_port=puerto, to_port=puerto,
        cidr_ipv4=cidr, referenced_security_group_id=origen, opts=op)


entrada("frontend_http", sg["frontend"].id, 80, cidr="0.0.0.0/0")
# El navegador llama directo a la API, por eso es pública
entrada("backend_api", sg["backend"].id, 8000, cidr="0.0.0.0/0")
# Prometheus del tenant: para la plataforma (admin_cidr) y para el Prometheus central
entrada("backend_prometheus_admin", sg["backend"].id, 9090, cidr=admin_cidr)
for sg_id in sg_monitoreo.ids:
    entrada(f"backend_prometheus_monitoreo-{sg_id}", sg["backend"].id, 9090, origen=sg_id)
# PostgreSQL solo desde el backend de ESTE tenant (aislamiento de datos)
entrada("bd_postgres", sg["bd"].id, 5432, origen=sg["backend"].id)
# Exportadores de métricas: solo los lee el Prometheus del backend del tenant
for clave, (capa, puerto) in {"frontend_node": ("frontend", 9100), "bd_node": ("bd", 9100),
                              "bd_postgres": ("bd", 9187)}.items():
    entrada(f"exportadores-{clave}", sg[capa].id, puerto, origen=sg["backend"].id)
for capa, grupo_sg in sg.items():
    entrada(f"ssh-{capa}", grupo_sg.id, 22, cidr=admin_cidr)
    aws.vpc.SecurityGroupEgressRule(f"all-{capa}", security_group_id=grupo_sg.id, cidr_ipv4="0.0.0.0/0",
                                    ip_protocol="-1", opts=op)


# ---------- Instancias (configuración inicial con cloud-init) ----------
CLOUD_INIT = Path(__file__).parent / "cloud-init"


def cloud_init(archivo: str, valores: dict) -> str:
    """Renderiza las plantillas .tftpl compartidas con Terraform (solo usan ${variable})."""
    texto = (CLOUD_INIT / archivo).read_text(encoding="utf-8")
    return re.sub(r"\$\{(\w+)\}", lambda m: str(valores[m.group(1)]), texto)


def instancia(capa, tipo, disco, grupo_sg, user_data, extra_tags=None):
    # ami y user_data se ignoran después de crear: una AMI nueva o un cambio en cloud-init
    # NO debe reemplazar la máquina (se perderían los datos de la BD).
    return aws.ec2.Instance(
        capa, ami=ami(f"{capa}_ami"), instance_type=tipo, subnet_id=subnet_id,
        vpc_security_group_ids=[grupo_sg.id], key_name=par_claves.key_name,
        tags={"Name": f"{nombre}-{capa}", "Rol": capa, **(extra_tags or {})},
        volume_tags={"Name": f"{nombre}-{capa}", "Rol": capa, "Tenant": tenant_id, "Entorno": entorno},
        root_block_device=aws.ec2.InstanceRootBlockDeviceArgs(volume_size=disco, volume_type="gp3", encrypted=True),
        user_data=user_data, opts=opciones(ignore_changes=["ami", "userData"]))


bd = instancia("bd", tipo_bd, disco_bd_gb, sg["bd"], pass_db.result.apply(lambda clave: cloud_init(
    "bd.yaml.tftpl", {"hostname": f"{nombre}-bd", "db_name": db_name, "db_user": db_user,
                      "db_password": clave, "vpc_cidr": vpc.cidr_block})))
# El Prometheus central descubre los backends por estas etiquetas (ec2_sd)
backend = instancia("backend", tipo_backend, disco_app_gb, sg["backend"], bd.private_ip.apply(
    lambda ip: cloud_init("backend.yaml.tftpl", {"hostname": f"{nombre}-backend", "bd_ip": ip})))
frontend = instancia("frontend", tipo_frontend, disco_app_gb, sg["frontend"],
                     cloud_init("frontend.yaml.tftpl", {"hostname": f"{nombre}-frontend"}))

# IP fijas: las URL del tenant no cambian al suspender y reanudar
eip_frontend = aws.ec2.Eip("frontend", domain="vpc", instance=frontend.id, tags={"Name": f"{nombre}-frontend"}, opts=op)
eip_backend = aws.ec2.Eip("backend", domain="vpc", instance=backend.id, tags={"Name": f"{nombre}-backend"}, opts=op)

# ---------- Suspensión / reanudación ----------
# Suspender = detener las 3 instancias (se conservan discos, IP elásticas y estado).
estado_bd = aws.ec2transitgateway.InstanceState("bd", instance_id=bd.id, state=estado, opts=op)
estado_backend = aws.ec2transitgateway.InstanceState("backend", instance_id=backend.id, state=estado,
                                                     opts=opciones(depends_on=[estado_bd]))
aws.ec2transitgateway.InstanceState("frontend", instance_id=frontend.id, state=estado,
                                    opts=opciones(depends_on=[estado_backend]))

# ---------- Salidas (mismos nombres que outputs.tf) ----------
sufijo = f".{dominio_base}" if dominio_base else ""
pulumi.export("frontend_url", eip_frontend.public_ip.apply(lambda ip: f"http://{tenant_id}.{ip}{sufijo}"))
pulumi.export("frontend_ip_url", eip_frontend.public_ip.apply(lambda ip: f"http://{ip}"))
pulumi.export("backend_url", eip_backend.public_ip.apply(lambda ip: f"http://{ip}:8000/api"))
pulumi.export("prometheus_url", eip_backend.public_ip.apply(lambda ip: f"http://{ip}:9090"))
pulumi.export("hosts", {
    "frontend": {"publica": eip_frontend.public_ip, "privada": frontend.private_ip},
    "backend": {"publica": eip_backend.public_ip, "privada": backend.private_ip},
    "bd": {"publica": bd.public_ip, "privada": bd.private_ip},
})
pulumi.export("instancias", {"frontend": frontend.id, "backend": backend.id, "bd": bd.id})
pulumi.export("bd_volumen_id", bd.root_block_device.apply(lambda r: r.volume_id))
pulumi.export("amis", {"frontend": frontend.ami, "backend": backend.ami, "bd": bd.ami})
pulumi.export("monitoreo_conectado", len(sg_monitoreo.ids) > 0)

# ---------- Secretos: nunca se guardan en la base de la plataforma ----------
pulumi.export("secretos", pulumi.Output.secret(pulumi.Output.all(
    db_name=db_name, db_user=db_user, db_password=pass_db.result, django_secret=django_secret.result,
    admin_password=admin_app.result, ssh_privada=clave_ansible.private_key_openssh)))

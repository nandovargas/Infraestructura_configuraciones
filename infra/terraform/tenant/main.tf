locals {
  nombre    = "asist-${var.tenant_id}-${var.entorno}"
  subnet_id = sort(data.aws_subnets.default.ids)[0]
}

# ---------- Red: VPC y subredes por defecto ----------
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Grupo de seguridad de la instancia de monitoreo (si ya está desplegada).
# Su Prometheus central federa las métricas del Prometheus de este tenant.
data "aws_security_groups" "monitoreo" {
  filter {
    name   = "tag:Plataforma"
    values = ["asistencia-saas"]
  }
  filter {
    name   = "tag:Rol"
    values = ["monitoreo"]
  }
}

# ---------- AMIs creadas con Packer ----------
data "aws_ami" "frontend" {
  owners      = ["self"]
  most_recent = true
  filter {
    name   = "name"
    values = ["frontend_ami"]
  }
}

data "aws_ami" "backend" {
  owners      = ["self"]
  most_recent = true
  filter {
    name   = "name"
    values = ["backend_ami"]
  }
}

data "aws_ami" "bd" {
  owners      = ["self"]
  most_recent = true
  filter {
    name   = "name"
    values = ["bd_ami"]
  }
}

# ---------- Secretos del tenant (solo viven en el estado cifrado) ----------
resource "random_password" "db" {
  length  = 24
  special = false
}

resource "random_password" "django_secret" {
  length  = 50
  special = false
}

resource "random_password" "admin_app" {
  length  = 16
  special = false
}

resource "tls_private_key" "ansible" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "ansible" {
  key_name_prefix = "${local.nombre}-"
  public_key      = tls_private_key.ansible.public_key_openssh
}

# ---------- Grupos de seguridad (uno por capa y por tenant) ----------
resource "aws_security_group" "frontend" {
  name_prefix = "${local.nombre}-frontend-"
  description = "Frontend ${local.nombre}: HTTP publico"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${local.nombre}-frontend", Rol = "frontend" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "backend" {
  name_prefix = "${local.nombre}-backend-"
  description = "Backend ${local.nombre}: API Django y Prometheus"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${local.nombre}-backend", Rol = "backend" }
  lifecycle { create_before_destroy = true }
}

resource "aws_security_group" "bd" {
  name_prefix = "${local.nombre}-bd-"
  description = "BD ${local.nombre}: PostgreSQL solo desde su backend"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "${local.nombre}-bd", Rol = "bd" }
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "frontend_http" {
  security_group_id = aws_security_group.frontend.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

# El navegador llama directo a la API, por eso es pública
resource "aws_vpc_security_group_ingress_rule" "backend_api" {
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 8000
  to_port           = 8000
}

# Prometheus del tenant: para la plataforma (admin_cidr) y para el Prometheus central
resource "aws_vpc_security_group_ingress_rule" "backend_prometheus_admin" {
  security_group_id = aws_security_group.backend.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 9090
  to_port           = 9090
}

resource "aws_vpc_security_group_ingress_rule" "backend_prometheus_monitoreo" {
  for_each                     = toset(data.aws_security_groups.monitoreo.ids)
  security_group_id            = aws_security_group.backend.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 9090
  to_port                      = 9090
}

# PostgreSQL solo desde el backend de ESTE tenant (aislamiento de datos)
resource "aws_vpc_security_group_ingress_rule" "bd_postgres" {
  security_group_id            = aws_security_group.bd.id
  referenced_security_group_id = aws_security_group.backend.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

# Exportadores de métricas: solo los lee el Prometheus del backend del tenant
resource "aws_vpc_security_group_ingress_rule" "exportadores" {
  for_each = {
    frontend_node = { sg = aws_security_group.frontend.id, puerto = 9100 }
    bd_node       = { sg = aws_security_group.bd.id, puerto = 9100 }
    bd_postgres   = { sg = aws_security_group.bd.id, puerto = 9187 }
  }
  security_group_id            = each.value.sg
  referenced_security_group_id = aws_security_group.backend.id
  ip_protocol                  = "tcp"
  from_port                    = each.value.puerto
  to_port                      = each.value.puerto
}

resource "aws_vpc_security_group_ingress_rule" "ssh" {
  for_each = {
    frontend = aws_security_group.frontend.id
    backend  = aws_security_group.backend.id
    bd       = aws_security_group.bd.id
  }
  security_group_id = each.value
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = 22
  to_port           = 22
}

resource "aws_vpc_security_group_egress_rule" "all" {
  for_each = {
    frontend = aws_security_group.frontend.id
    backend  = aws_security_group.backend.id
    bd       = aws_security_group.bd.id
  }
  security_group_id = each.value
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# ---------- Instancias (configuración inicial con cloud-init) ----------
# ami y user_data se ignoran después de crear: una AMI nueva de Packer o un cambio
# en cloud-init NO debe reemplazar la máquina (se perderían los datos de la BD).
resource "aws_instance" "bd" {
  ami                    = data.aws_ami.bd.id
  instance_type          = var.tipo_bd
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.bd.id]
  key_name               = aws_key_pair.ansible.key_name
  tags                   = { Name = "${local.nombre}-bd", Rol = "bd" }
  volume_tags            = { Name = "${local.nombre}-bd", Rol = "bd", Tenant = var.tenant_id, Entorno = var.entorno }

  root_block_device {
    volume_size = var.disco_bd_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/cloud-init/bd.yaml.tftpl", {
    hostname    = "${local.nombre}-bd"
    db_name     = var.db_name
    db_user     = var.db_user
    db_password = random_password.db.result
    vpc_cidr    = data.aws_vpc.default.cidr_block
  })

  lifecycle { ignore_changes = [ami, user_data] }
}

resource "aws_instance" "backend" {
  ami                    = data.aws_ami.backend.id
  instance_type          = var.tipo_backend
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.backend.id]
  key_name               = aws_key_pair.ansible.key_name
  # El Prometheus central descubre los backends por estas etiquetas (ec2_sd)
  tags        = { Name = "${local.nombre}-backend", Rol = "backend" }
  volume_tags = { Name = "${local.nombre}-backend", Rol = "backend", Tenant = var.tenant_id, Entorno = var.entorno }

  root_block_device {
    volume_size = var.disco_app_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/cloud-init/backend.yaml.tftpl", {
    hostname = "${local.nombre}-backend"
    bd_ip    = aws_instance.bd.private_ip
  })

  lifecycle { ignore_changes = [ami, user_data] }
}

resource "aws_instance" "frontend" {
  ami                    = data.aws_ami.frontend.id
  instance_type          = var.tipo_frontend
  subnet_id              = local.subnet_id
  vpc_security_group_ids = [aws_security_group.frontend.id]
  key_name               = aws_key_pair.ansible.key_name
  tags                   = { Name = "${local.nombre}-frontend", Rol = "frontend" }
  volume_tags            = { Name = "${local.nombre}-frontend", Rol = "frontend", Tenant = var.tenant_id, Entorno = var.entorno }

  root_block_device {
    volume_size = var.disco_app_gb
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = templatefile("${path.module}/cloud-init/frontend.yaml.tftpl", {
    hostname = "${local.nombre}-frontend"
  })

  lifecycle { ignore_changes = [ami, user_data] }
}

# IP fijas: las URL del tenant no cambian al suspender y reanudar
resource "aws_eip" "frontend" {
  domain   = "vpc"
  instance = aws_instance.frontend.id
  tags     = { Name = "${local.nombre}-frontend" }
}

resource "aws_eip" "backend" {
  domain   = "vpc"
  instance = aws_instance.backend.id
  tags     = { Name = "${local.nombre}-backend" }
}

# ---------- Suspensión / reanudación ----------
# Política: suspender = detener las 3 instancias. Se conservan discos (datos), IP elásticas
# y estado; se deja de pagar cómputo. Reanudar = volver a "running".
resource "aws_ec2_instance_state" "bd" {
  instance_id = aws_instance.bd.id
  state       = var.estado
}

resource "aws_ec2_instance_state" "backend" {
  instance_id = aws_instance.backend.id
  state       = var.estado
  depends_on  = [aws_ec2_instance_state.bd]
}

resource "aws_ec2_instance_state" "frontend" {
  instance_id = aws_instance.frontend.id
  state       = var.estado
  depends_on  = [aws_ec2_instance_state.backend]
}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Ubuntu 24.04 LTS oficial de Canonical (la misma base que las AMIs de Packer)
data "aws_ami" "ubuntu" {
  owners      = ["099720109477"]
  most_recent = true
  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"]
  }
}

resource "random_password" "grafana_admin" {
  length  = 20
  special = false
}

resource "tls_private_key" "ansible" {
  algorithm = "ED25519"
}

resource "aws_key_pair" "ansible" {
  key_name_prefix = "asist-monitoreo-"
  public_key      = tls_private_key.ansible.public_key_openssh
}

# Los tenants buscan este SG por sus etiquetas (Plataforma + Rol) para abrirle su puerto 9090
resource "aws_security_group" "monitoreo" {
  name_prefix = "asist-monitoreo-"
  description = "Monitoreo: Grafana y Prometheus central"
  vpc_id      = data.aws_vpc.default.id
  tags        = { Name = "asist-monitoreo" }
  lifecycle { create_before_destroy = true }
}

resource "aws_vpc_security_group_ingress_rule" "admin" {
  for_each          = { ssh = 22, grafana = 3000, prometheus = 9090 }
  security_group_id = aws_security_group.monitoreo.id
  cidr_ipv4         = var.admin_cidr
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
}

resource "aws_vpc_security_group_egress_rule" "all" {
  security_group_id = aws_security_group.monitoreo.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

# Permiso mínimo para que Prometheus descubra los backends de los tenants (ec2_sd_configs)
resource "aws_iam_role" "monitoreo" {
  name_prefix = "asist-monitoreo-"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "descubrimiento" {
  role = aws_iam_role.monitoreo.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["ec2:DescribeInstances", "ec2:DescribeAvailabilityZones"]
      Resource = "*"
    }]
  })
}

resource "aws_iam_instance_profile" "monitoreo" {
  name_prefix = "asist-monitoreo-"
  role        = aws_iam_role.monitoreo.name
}

resource "aws_instance" "monitoreo" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.tipo_instancia
  subnet_id              = sort(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.monitoreo.id]
  key_name               = aws_key_pair.ansible.key_name
  iam_instance_profile   = aws_iam_instance_profile.monitoreo.name
  tags                   = { Name = "asist-monitoreo" }

  # IMDSv2 obligatorio (Prometheus corre en el host, no en un contenedor)
  metadata_options {
    http_tokens = "required"
  }

  root_block_device {
    volume_size = 20
    volume_type = "gp3"
    encrypted   = true
  }

  user_data = file("${path.module}/cloud-init/monitoreo.yaml")
  lifecycle { ignore_changes = [ami, user_data] }
}

resource "aws_eip" "monitoreo" {
  domain   = "vpc"
  instance = aws_instance.monitoreo.id
  tags     = { Name = "asist-monitoreo" }
}

output "grafana_url" {
  value = "http://${aws_eip.monitoreo.public_ip}:3000"
}

output "prometheus_url" {
  value = "http://${aws_eip.monitoreo.public_ip}:9090"
}

output "host" {
  value = { publica = aws_eip.monitoreo.public_ip, privada = aws_instance.monitoreo.private_ip }
}

output "region" {
  value = var.region
}

output "secretos" {
  sensitive = true
  value = {
    grafana_admin = random_password.grafana_admin.result
    ssh_privada   = tls_private_key.ansible.private_key_openssh
  }
}

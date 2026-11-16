packer {
  required_version = ">= 1.10.0"
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

# Las credenciales NO se declaran aquí: el plugin amazon lee
# AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY del entorno.

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

# Versiones de lenguajes y servicios
variable "node_major" {
  type    = string
  default = "22" # LTS; Vite 8 requiere Node >= 20.19 / 22.12
}

variable "prometheus_version" {
  type    = string
  default = "3.5.0" # rama LTS
}

# Ubuntu 24.04 LTS (Noble), 64 bits (amd64), publicada por Canonical.
# Trae Python 3.12 y PostgreSQL 16 en sus repositorios.
data "amazon-ami" "ubuntu" {
  region      = var.region
  most_recent = true
  owners      = ["099720109477"]
  filters = {
    name                = "ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*"
    root-device-type    = "ebs"
    virtualization-type = "hvm"
  }
}

source "amazon-ebs" "base" {
  region                  = var.region
  instance_type           = var.instance_type
  source_ami              = data.amazon-ami.ubuntu.id
  ssh_username            = "ubuntu"
  temporary_key_pair_type = "ed25519"

  launch_block_device_mappings {
    device_name           = "/dev/sda1"
    volume_size           = 16
    volume_type           = "gp3"
    delete_on_termination = true
  }

  tags = {
    Proyecto = "asistencia"
    SO       = "Ubuntu 24.04 LTS amd64"
  }
}

# ---------- frontend_ami ----------
build {
  name = "frontend"

  source "amazon-ebs.base" {
    name     = "frontend"
    ami_name = "frontend_ami"
  }

  provisioner "shell" {
    inline = ["mkdir -p /tmp/app"]
  }
  provisioner "file" {
    sources     = ["${path.root}/../asistencia_frontend/package.json", "${path.root}/../asistencia_frontend/package-lock.json"]
    destination = "/tmp/app/"
  }
  provisioner "shell" {
    script           = "${path.root}/scripts/frontend.sh"
    environment_vars = ["NODE_MAJOR=${var.node_major}"]
    execute_command  = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
  }
}

# ---------- backend_ami (Django + Prometheus) ----------
build {
  name = "backend"

  source "amazon-ebs.base" {
    name     = "backend"
    ami_name = "backend_ami"
  }

  provisioner "shell" {
    inline = ["mkdir -p /tmp/app"]
  }
  provisioner "file" {
    source      = "${path.root}/../asistencia_backend/requirements.txt"
    destination = "/tmp/app/requirements.txt"
  }
  provisioner "file" {
    source      = "${path.root}/files/prometheus.yml"
    destination = "/tmp/app/prometheus.yml"
  }
  provisioner "shell" {
    script           = "${path.root}/scripts/backend.sh"
    environment_vars = ["PROMETHEUS_VERSION=${var.prometheus_version}"]
    execute_command  = "{{ .Vars }} sudo -E bash '{{ .Path }}'"
  }
}

# ---------- bd_ami (PostgreSQL) ----------
build {
  name = "bd"

  source "amazon-ebs.base" {
    name     = "bd"
    ami_name = "bd_ami"
  }

  provisioner "shell" {
    script          = "${path.root}/scripts/bd.sh"
    execute_command = "sudo -E bash '{{ .Path }}'"
  }
}

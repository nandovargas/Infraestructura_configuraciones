# Instancia de monitoreo compartida: Grafana + Prometheus central (federación de los tenants).
terraform {
  required_version = ">= 1.10.0"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = {
      Plataforma = "asistencia-saas"
      Rol        = "monitoreo"
      Gestion    = "terraform"
    }
  }
}

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "tipo_instancia" {
  type    = string
  default = "t3.small"
}

variable "admin_cidr" {
  description = "CIDR con acceso a Grafana (3000), Prometheus (9090) y SSH (Ansible)."
  type        = string
}

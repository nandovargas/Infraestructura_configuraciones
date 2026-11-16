# Stack de UN tenant. La plataforma copia esta carpeta a su directorio de trabajo
# y genera backend.tf (estado en S3 cifrado con bloqueo, o local en modo desarrollo).
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

# Credenciales: el proveedor lee AWS_ACCESS_KEY_ID y AWS_SECRET_ACCESS_KEY del entorno.
provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Plataforma = "asistencia-saas"
      Tenant     = var.tenant_id
      Entorno    = var.entorno
      Plan       = var.plan
      Gestion    = var.gestion
    }
  }
}

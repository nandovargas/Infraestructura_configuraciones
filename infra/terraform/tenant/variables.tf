variable "region" {
  type    = string
  default = "us-east-1"
}

variable "tenant_id" {
  description = "Identificador único del tenant (minúsculas, números y guiones)."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.tenant_id))
    error_message = "tenant_id: 3-21 caracteres, empieza con letra, solo a-z, 0-9 y guiones."
  }
}

variable "entorno" {
  type = string
  validation {
    condition     = contains(["dev", "staging", "prod"], var.entorno)
    error_message = "entorno debe ser dev, staging o prod."
  }
}

variable "plan" {
  description = "Nombre del plan (solo para etiquetas)."
  type        = string
}

variable "tipo_frontend" {
  type = string
}

variable "tipo_backend" {
  type = string
}

variable "tipo_bd" {
  type = string
}

variable "disco_app_gb" {
  description = "Disco raíz de frontend y backend (mínimo 16, tamaño de la AMI)."
  type        = number
}

variable "disco_bd_gb" {
  description = "Disco de la instancia de base de datos."
  type        = number
}

variable "estado" {
  description = "running = activo, stopped = suspendido (los discos y las IP elásticas se conservan)."
  type        = string
  default     = "running"
  validation {
    condition     = contains(["running", "stopped"], var.estado)
    error_message = "estado debe ser running o stopped."
  }
}

variable "admin_cidr" {
  description = "CIDR desde donde la plataforma entra por SSH (Ansible) y consulta Prometheus."
  type        = string
}

variable "db_name" {
  type    = string
  default = "asistencia_db"
}

variable "db_user" {
  type    = string
  default = "asistencia"
}

variable "gestion" {
  description = "Motor que administra el stack (terraform u opentofu); solo para la etiqueta Gestion."
  type        = string
  default     = "terraform"
}

variable "dominio_base" {
  description = "Sufijo DNS de la URL del tenant (nip.io = DNS comodín público). Vacío = http://<tenant_id>.<IP>."
  type        = string
  default     = "nip.io"
}

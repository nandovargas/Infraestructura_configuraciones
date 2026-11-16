# Asistencia SaaS · plataforma de tenants

Plataforma (Python + Streamlit) para crear y administrar **tenants** de la aplicación de asistencia.
Cada tenant es una institución educativa con su **propia infraestructura y su propia base de datos**:
sus usuarios, módulos (cursos), estudiantes y asistencias no comparten nada con los de otro tenant.

```
┌──────────────── Plataforma (esta carpeta, corre en la máquina del administrador) ───────────────┐
│  Streamlit (app.py) ──► SQLite (datos/plataforma.db) ◄── worker.py (trabajos en segundo plano)  │
│                                                          │  Terraform  ─► AWS (estado por stack)  │
│                                                          │  Ansible (Docker) ─► EC2 por SSH      │
│                                                          │  boto3 ─► snapshots de retención       │
└──────────────────────────────────────────────────────────┼─────────────────────────────────────┘
          ┌───────────────────── AWS (VPC por defecto) ─────┴──────────────────────────┐
          │ Tenant A: frontend ─ backend(+Prometheus) ─ BD      ◄─┐ federación (9090)   │
          │ Tenant B: frontend ─ backend(+Prometheus) ─ BD      ◄─┤                     │
          │ Monitoreo: Prometheus central (reglas de alerta) + Grafana ──┘              │
          └────────────────────────────────────────────────────────────────────────────┘
```

## Puesta en marcha

Requisitos: Python 3.12+, Terraform ≥ 1.10 (y opcionalmente OpenTofu ≥ 1.10 y/o Pulumi 3), Docker Desktop abierto, las AMIs de Packer
(`packer build packer/imagenes.pkr.hcl`) y credenciales de AWS en el entorno
(`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, opcional `AWS_REGION`).

```powershell
cd plataforma
.\iniciar.ps1          # crea el venv, inicia el worker en segundo plano y abre http://localhost:8501
```

La primera vez se crea el usuario administrador (hash scrypt en `datos/admin.json`).
Pruebas: `.\.venv\Scripts\python -m pytest`.

| Variable de entorno | Para qué |
|---|---|
| `AWS_REGION` | Región (por defecto `us-east-1`; debe ser la de las AMIs) |
| `PLATAFORMA_ADMIN_CIDR` | CIDR fijo para SSH/Prometheus (si no, se detecta la IP pública en cada operación) |
| `PLATAFORMA_TRABAJOS_PARALELOS` | Operaciones simultáneas del worker (3) |
| `PLATAFORMA_DATOS` | Carpeta de datos (por defecto `plataforma/datos`) |

## Qué hace cada parte

| Carpeta | Contenido |
|---|---|
| `app.py`, `paginas/` | Interfaz: resumen, planes, tenants, detalle, operaciones, salud/alertas, infraestructura |
| `paginas/analisis.py`, `ejecucion/kics.py` | Análisis de seguridad del código de infraestructura con Checkmarx KICS: reporte al crear un tenant, consulta e historial, descargas |
| `worker.py` | Ejecuta la cola de operaciones; sincroniza alertas (1 min) y purga snapshots vencidos (6 h) |
| `nucleo/` | Base de datos, reglas de negocio, protección de secretos, traducción de errores, versiones, métricas |
| `motores/` | Interfaz común de motores IaC: Terraform, OpenTofu y Pulumi implementados; CloudFormation registrado como "próximamente" |
| `../infra/pulumi/tenant` | Programa Pulumi (Python) equivalente al stack Terraform del tenant |
| `ejecucion/` | Manejadores de cada operación, Ansible en Docker, AWS (boto3) |
| `../infra/terraform/tenant` | Stack de un tenant: 3 EC2, SG, IP elásticas, encendido/apagado, secretos |
| `../infra/terraform/monitoreo` | Instancia de Grafana + Prometheus central con rol IAM de descubrimiento |
| `../infra/terraform/estado` | Bucket S3 para el estado de Terraform |
| `../ansible` | `sitio.yml` (tenant) y `monitoreo.yml`; roles reutilizables |

## URL de cada tenant

El identificador único del tenant se usa como subdominio de la IP fija de su frontend:

| Identificador | IP del frontend | URL del panel |
|---|---|---|
| `upsa` | 54.172.11.111 | `http://upsa.54.172.11.111.nip.io/panel/login` |
| `uagrm` | 44.215.224.136 | `http://uagrm.44.215.224.136.nip.io/panel/login` |

`nip.io` es un DNS comodín público (`<lo-que-sea>.<IP>.nip.io` resuelve a `<IP>`), así la URL funciona
sin registrar un dominio. Se cambia con la variable de entorno `PLATAFORMA_DOMINIO`; vacía, la URL es
exactamente `http://upsa.54.172.11.111/panel/login`, que solo resuelve si se agrega ese nombre a un DNS
propio o al archivo `hosts`. El backend acepta (CORS) tanto la URL del tenant como el acceso directo por IP.
Para tenants existentes, la URL nueva se aplica al volver a **Desplegar**.

## Ciclo de vida

| Operación | Qué hace | Estado final |
|---|---|---|
| Crear / desplegar | `terraform apply` del stack del tenant → Ansible: BD, Django + gunicorn, Prometheus y exportadores, build de React en nginx → verificación de punta a punta | `activo` |
| Cambiar plan | Igual que desplegar con otro plan: cambia tipos de instancia y amplía discos (no los reduce) | `activo` |
| Suspender | Snapshot de la BD (retención del plan) → `estado = stopped` en Terraform | `suspendido` |
| Reanudar | `estado = running` → refresco del estado → verificación (frontend, login API, métricas) | `activo` |
| Eliminar | Vista previa (`terraform state list` + datos afectados) → confirmación escribiendo el ID → snapshot final opcional → `terraform destroy` | `eliminado` |

**Política de suspensión**: se detienen las 3 instancias (no se paga cómputo). Se conservan discos,
IP elásticas (las URL no cambian), credenciales y estado. Antes de detener se toma un snapshot de la BD
que se conserva los días de retención del plan.

**Política de retención**: los snapshots (suspensión y eliminación) llevan la etiqueta `EliminarDespues`.
El worker borra los vencidos. El registro del tenant y su historial nunca se borran y su
identificador queda reservado.

## Operaciones seguras y repetibles

- **Una operación activa por tenant**: la cola lo garantiza en una transacción (`BEGIN IMMEDIATE`). Además,
  Terraform bloquea el estado (S3 `use_lockfile` o el bloqueo del backend local).
- **Reintentar no duplica**: Terraform compara con su estado y solo crea lo que falta. Ansible es idempotente.
  La AMI y el `user_data` se ignoran después de crear (`ignore_changes`), así una AMI nueva no reemplaza la BD.
- **Trabajos en segundo plano**: el worker es un proceso aparte, con un único worker por máquina (bloqueo de
  archivo). Si se detiene a mitad de una operación, al arrancar la marca como fallida y reintentable.
- **Historial**: la tabla `operaciones` guarda parámetros, intento, paso, progreso, resultado y error. Cada
  operación tiene su registro completo en `datos/logs/operacion-N.log`. La tabla `despliegues` registra
  las versiones (ver abajo).

### Versiones registradas en cada despliegue
- **Aplicación**: commit de git (`-modificado` si hay cambios sin confirmar) más la huella sha256 del contenido
  realmente desplegado, para backend y frontend.
- **Plantillas**: huella sha256 de `infra/terraform/<stack>` + `ansible/` (`tpl-xxxxxxxxxxxx`).
- **Herramientas**: Terraform y proveedores (del `.terraform.lock.hcl`), ansible-core, Docker, Python,
  Streamlit, boto3 y la versión de la plataforma.
- **AMIs** de Packer usadas por cada instancia y el plan aplicado.

## Estado de Terraform: dónde se guarda y cómo se protege

Cada stack tiene **su propio estado**: uno por tenant (`tenants/<id>/terraform.tfstate`), uno para el
monitoreo y uno para el bucket. El estado contiene los secretos del tenant (contraseña de la BD, clave de Django,
contraseña del admin de la app, clave SSH). Es la **única** copia de esos secretos: la base de la plataforma
no los guarda.

| Modo | Dónde | Protección |
|---|---|---|
| **S3 (producción)** | `s3://asist-saas-tfstate-<sufijo>/` (botón «Preparar almacenamiento S3» en Infraestructura) | Cifrado en reposo SSE-KMS · `encrypt = true` · bloqueo de acceso público · política que **niega conexiones sin TLS** · versionado (se puede volver a un estado anterior) · versiones antiguas expiran a 90 días · `prevent_destroy` en el bucket · **bloqueo nativo de S3** (`use_lockfile = true`) para que dos procesos no escriban a la vez |
| **Local (desarrollo)** | `plataforma/datos/estado/` | Solo en la máquina de la plataforma, fuera de git (`.gitignore`). Proteja la carpeta (permisos de usuario, disco cifrado/BitLocker) y haga copias de seguridad |

- El tipo de backend se fija al crear cada tenant y se guarda en su registro. Cambiar a S3 no mueve los
  tenants existentes. Para migrar uno: `terraform init -migrate-state` en `datos/trabajo/tenants/<id>`.
- El estado del bucket (bootstrap) es local y no contiene secretos.
- Acceso: los permisos de IAM del usuario de la plataforma limitan quién puede leer el bucket. Se recomienda
  un usuario o rol dedicado con acceso solo a `asist-saas-tfstate-*` y a los recursos etiquetados
  `Plataforma=asistencia-saas`.

## Secretos y credenciales

- No hay secretos en el código ni en el repositorio. Credenciales de AWS solo en el entorno. Secretos de
  tenants generados por Terraform (`random_password`, `tls_private_key`) y guardados solo en el estado.
- El inventario de Ansible y la clave SSH se escriben en `datos/tmp/<uuid>/` solo mientras dura la operación y se
  borran al terminar. El contenedor de Ansible solo monta `ansible/` y el código de la app, no `datos/`.
- **Registros y errores sin secretos**: cada línea pasa por un redactor que reemplaza los valores secretos
  conocidos de la operación y patrones típicos (claves privadas, `password=`, IDs de clave AWS, URLs con
  contraseña). La interfaz traduce los errores a mensajes comprensibles con una sugerencia.
- Las credenciales del admin de cada app y de Grafana se leen del estado solo al pulsar «Mostrar».
- Streamlit escucha solo en `127.0.0.1`. El acceso a SSH y a Prometheus de las instancias se limita a la
  IP pública de la plataforma.

## Aislamiento de datos entre tenants

- Instancias, discos (cifrados) y base de datos dedicados por tenant.
- El grupo de seguridad de la BD solo admite el puerto 5432 desde el grupo del backend **del mismo tenant**.
  Los exportadores (9100/9187) solo aceptan conexiones desde ese backend.
- Credenciales de BD, clave de Django y admin distintas por tenant.
- Las métricas se etiquetan con `tenant`/`entorno` y el Prometheus de cada tenant solo acepta conexiones de la
  plataforma y del monitoreo.

## Salud, métricas y alertas

- Cada backend tiene un Prometheus propio que lee Django (`django-prometheus`), node_exporter de las 3
  máquinas y postgres_exporter. Sus series llevan las etiquetas `tenant`, `entorno` y `plan`.
- La instancia de **monitoreo** (aparte) tiene un Prometheus central. Ese Prometheus **descubre solo** los
  backends por etiquetas EC2 (`ec2_sd_configs`, solo instancias en ejecución), federa sus métricas y evalúa
  las reglas de alerta. **Grafana** trae el dashboard «Salud de los tenants» con filtros por entorno y tenant:
  disponibilidad, servicios caídos, CPU, memoria, disco, latencia p50/p95, peticiones, errores 5xx,
  estado/conexiones/tamaño de la BD y alertas activas.
- **Alertas**: tenant inalcanzable, servicio caído, BD caída, errores 5xx > 5 %, latencia p95 > 1 s,
  CPU > 85 %, memoria > 90 %, disco > 85 %, conexiones BD > 80 %. El worker las sincroniza cada minuto como
  eventos, con el tenant afectado y la hora de detección (y de resolución).
- Si un tenant se crea antes que el monitoreo, «Conectarlos» en Infraestructura vuelve a desplegarlo para
  abrir su Prometheus al monitoreo.

## Motores de aprovisionamiento

Al crear un tenant se elige el motor; queda fijo para toda su vida (su estado vive en ese motor).

| Motor | Plantilla | CLI | Notas |
|---|---|---|---|
| Terraform | `infra/terraform/tenant` (HCL) | `terraform` ≥ 1.10 | Motor por defecto. También aprovisiona el monitoreo y el bucket del estado |
| OpenTofu | la misma plantilla HCL | `tofu` ≥ 1.10 (`winget install OpenTofu.Tofu`) | Proveedores desde registry.opentofu.org. Cifrado de estado opcional con `TF_ENCRYPTION` |
| Pulumi | `infra/pulumi/tenant` (Python) | `pulumi` 3 (`winget install Pulumi.Pulumi`) | Mismos recursos, nombres y salidas; reutiliza las plantillas cloud-init del stack HCL |

- **Estado**: el mismo destino para los tres (carpeta `datos/estado` o el bucket S3). Pulumi usa un backend
  "DIY": `datos/estado/tenants/<id>.pulumi/` o `s3://<bucket>/pulumi/tenants/<id>`. Un stack por tenant.
- **Secretos de Pulumi**: se cifran en el estado con la frase de `PULUMI_CONFIG_PASSPHRASE` o, si no está
  definida, con la que se genera en `datos/pulumi.frase`. **Respáldela**: sin ella no se pueden leer las
  contraseñas de los tenants creados con Pulumi.
- **Entorno Python de Pulumi**: `datos/cache/pulumi-venv`. Se crea solo y se reinstala cuando cambia
  `infra/pulumi/tenant/requirements.txt`.
- **Binarios**: se buscan en el PATH y en las rutas de instalación habituales (sirve para herramientas
  recién instaladas). Se pueden fijar con `PLATAFORMA_TERRAFORM`, `PLATAFORMA_TOFU` y `PLATAFORMA_PULUMI`.
- La página **Plataforma → Motores de aprovisionamiento** muestra qué motores están instalados y dónde.

## Seguridad del código de infraestructura (Checkmarx KICS)

La plataforma analiza con [KICS](https://kics.io) el código que despliega cada motor. Es análisis estático:
no usa credenciales de AWS ni modifica nada.

| Código | Motores | Qué analiza KICS |
|---|---|---|
| `infra/terraform/<stack>` (HCL) | Terraform y OpenTofu (mismos archivos, mismo reporte) | Los `.tf` con las reglas de Terraform (más de mil) |
| `infra/pulumi/<stack>` (Python) | Pulumi | Un `Pulumi.yaml` generado desde el programa (KICS solo entiende Pulumi en YAML) |

- **Instalación**: KICS se ejecuta en su imagen Docker oficial (`docker pull checkmarx/kics:v2.2.1`). Si falta,
  la interfaz ofrece el botón «Instalar KICS». Otra versión: variable `PLATAFORMA_KICS_IMAGEN`.
- **Al crear un tenant** (Tenants → Nuevo tenant → paso 3): reporte del código del motor elegido, con hallazgos
  por severidad, botón para analizar y descargas. Los hallazgos altos o críticos se avisan; no bloquean.
- **Consultar resultados**: página **Seguridad IaC**: nuevo análisis por código y stack, historial, filtros por
  severidad, categoría y texto, detalle por regla con el fragmento de código.
- **Descargar reportes**: HTML, PDF, JSON, SARIF y CSV de cualquier análisis guardado.
- **Dónde se guardan**: `datos/escaneos/<n>/` (reportes y copia del código analizado); se conservan los 15 más
  recientes por código y stack. Un análisis se marca como no vigente cuando el código cambia después.
- **Pulumi**: `ejecucion/pulumi_a_yaml.py` ejecuta el programa con el motor simulado de Pulumi (sin AWS) y
  vuelca sus recursos reales a YAML. KICS trae pocas reglas para Pulumi, así que su reporte es más corto que el
  de HCL aunque la infraestructura sea la misma: revise también el de Terraform / OpenTofu.

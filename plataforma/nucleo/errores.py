"""Traduce errores técnicos (Terraform/OpenTofu/Pulumi, AWS, Ansible, Docker) a mensajes comprensibles."""
import re


class ErrorOperacion(Exception):
    """Error controlado de una operación: resumen para la persona + detalle técnico (ya limpio)."""

    def __init__(self, resumen: str, detalle: str = ""):
        super().__init__(resumen)
        self.resumen = resumen
        self.detalle = detalle


# (patrón, mensaje, sugerencia)
REGLAS = [
    (r"InvalidClientTokenId|SignatureDoesNotMatch|ExpiredToken|UnrecognizedClientException|no valid credential sources|NoCredentialsError",
     "Las credenciales de AWS no son válidas o vencieron.",
     "Revise AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY en el entorno donde corre el worker y reinícielo."),
    (r"UnauthorizedOperation|AccessDenied|not authorized to perform",
     "La cuenta de AWS no tiene permisos para una de las acciones.",
     "Pida al administrador de AWS los permisos de EC2/IAM/S3 que indica el detalle."),
    (r"VcpuLimitExceeded|InstanceLimitExceeded",
     "Se alcanzó el límite de vCPU/instancias de la cuenta de AWS en la región.",
     "Suspenda o elimine tenants que no use, o solicite un aumento de cuota a AWS."),
    (r"AddressLimitExceeded",
     "Se alcanzó el límite de IP elásticas de la región (5 por defecto).",
     "Libere IP elásticas sin uso o solicite un aumento de cuota."),
    (r"InsufficientInstanceCapacity",
     "AWS no tiene capacidad para ese tipo de instancia en este momento.",
     "Reintente en unos minutos o use un plan con otro tipo de instancia."),
    (r"Your query returned no results|no matching EC2 Image found|InvalidAMIID",
     "No se encontraron las AMIs de Packer (frontend_ami, backend_ami, bd_ami) en la región.",
     "Ejecute 'packer build' en la carpeta packer/ con la misma región."),
    (r"Error acquiring the state lock|ConditionalCheckFailed|PreconditionFailed.*lock",
     "Otra operación tiene bloqueado el estado de Terraform/OpenTofu de este recurso.",
     "Espere a que termine. Si el worker se interrumpió, libere el bloqueo con 'terraform force-unlock' "
     "(o 'tofu force-unlock')."),
    (r"the stack is currently locked|stack is locked",
     "Otra operación tiene bloqueado el stack de Pulumi de este tenant.",
     "Espere a que termine. Si el worker se interrumpió, ejecute 'pulumi cancel' en la copia de trabajo del tenant."),
    (r"incorrect passphrase|failed to decrypt|passphrase must be set",
     "No se pudieron descifrar los secretos del estado de Pulumi.",
     "Use la misma frase de cifrado con la que se creó el stack (datos/pulumi.frase o PULUMI_CONFIG_PASSPHRASE)."),
    (r"NoSuchBucket|S3 bucket .* does not exist",
     "No existe el bucket S3 configurado para el estado de Terraform.",
     "Prepare el almacenamiento del estado desde la página Plataforma."),
    (r"error during connect|docker daemon|dockerDesktopLinuxEngine|Cannot connect to the Docker daemon|docker: command not found|No se encontró 'docker'",
     "Docker no está en ejecución en la máquina de la plataforma.",
     "Abra Docker Desktop y reintente la operación."),
    (r"No se encontró 'terraform'|terraform: command not found",
     "Terraform no está instalado o no está en el PATH.",
     "Instale Terraform >= 1.10 y reinicie el worker."),
    (r"No se encontró 'tofu'|tofu: command not found",
     "OpenTofu no está instalado o no está en el PATH.",
     "Instale OpenTofu >= 1.10 (winget install OpenTofu.Tofu) y reinicie el worker."),
    (r"No se encontró 'pulumi'|pulumi: command not found",
     "Pulumi no está instalado o no está en el PATH.",
     "Instale Pulumi 3 (winget install Pulumi.Pulumi) y reinicie el worker."),
    (r"No se pudieron instalar los paquetes de Python de Pulumi|ModuleNotFoundError: No module named 'pulumi",
     "Falta el entorno Python del programa Pulumi.",
     "Revise la conexión a internet y reintente: el entorno se crea solo en datos/cache/pulumi-venv."),
    (r"UNREACHABLE!|Connection timed out|Connection refused.*port 22|Permission denied \(publickey",
     "Ansible no pudo conectarse por SSH a una de las máquinas.",
     "Puede que su IP pública haya cambiado (la regla SSH usa la IP detectada). Reintente: se volverá a detectar."),
    (r"cloud-init.*(error|degraded)|'status: done' not in ci.stdout",
     "La configuración inicial (cloud-init) de una máquina terminó con errores.",
     "Revise /var/log/cloud-init-output.log en la instancia; reintentar suele bastar si fue un fallo de red."),
    (r"pg_isready|could not connect to server|connection to server .* failed",
     "El backend no pudo conectarse a la base de datos del tenant.",
     "Reintente el despliegue; si persiste, revise el estado de la instancia de BD."),
    (r"npm (ERR|error)|vite.*error",
     "Falló la compilación del frontend.",
     "Revise el detalle: suele ser un error en el código del frontend o falta de memoria."),
    (r"RequestLimitExceeded|Throttling",
     "AWS limitó temporalmente las peticiones (demasiadas seguidas).",
     "Reintente en unos minutos."),
]


def traducir(texto: str, por_defecto: str = "La operación falló.") -> tuple[str, str]:
    """Devuelve (mensaje, sugerencia) para el primer patrón que coincida."""
    for patron, mensaje, sugerencia in REGLAS:
        if re.search(patron, texto or "", re.I):
            return mensaje, sugerencia
    return por_defecto, "Revise el detalle técnico y el registro completo de la operación; puede reintentarla de forma segura."

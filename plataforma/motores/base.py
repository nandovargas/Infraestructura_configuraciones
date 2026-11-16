"""Interfaz común de los motores de aprovisionamiento."""
import glob
import os
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from nucleo import config

# Callback de progreso: (fracción 0..1, mensaje)
Progreso = Callable[[float, str], None]
Log = Callable[[str], None]


def buscar_binario(nombre: str, candidatos: tuple[str, ...] = ()) -> str | None:
    """Ruta del ejecutable: variable PLATAFORMA_<NOMBRE>, PATH o rutas de instalación habituales.

    Las rutas habituales cubren el caso de una herramienta recién instalada (winget/instalador)
    cuyo PATH nuevo todavía no ven los procesos que ya estaban abiertos.
    """
    explicito = config.BINARIOS.get(nombre)
    if explicito and Path(explicito).is_file():
        return explicito
    ruta = shutil.which(nombre)
    if ruta:
        return ruta
    for patron in candidatos:
        patron = os.path.expandvars(os.path.expanduser(patron))
        for encontrado in sorted(glob.glob(patron), reverse=True):
            if Path(encontrado).is_file():
                return encontrado
    return None


class Motor(ABC):
    nombre = "base"
    etiqueta = "Base"
    disponible = True
    binario = ""                     # ejecutable de la CLI
    candidatos: tuple[str, ...] = ()  # rutas de instalación habituales
    dir_plantillas: Path = config.PLANTILLAS_TF
    stacks: set[str] | None = None   # stacks que implementa (None = todos los de dir_plantillas)

    def __init__(self, plantilla: Path, trabajo: Path, backend: dict, log: Log, entorno_proc: dict | None = None):
        self.plantilla = plantilla      # carpeta de la plantilla (infra/<motor>/<stack>)
        self.trabajo = trabajo          # copia de trabajo propia del stack
        self.backend = backend          # dónde vive el estado
        self.log = log
        self.entorno_proc = entorno_proc or {}

    # ---------- información para la interfaz ----------
    @classmethod
    def ruta_binario(cls) -> str | None:
        return buscar_binario(cls.binario, cls.candidatos) if cls.binario else None

    @classmethod
    def instalado(cls) -> bool:
        return cls.ruta_binario() is not None

    @classmethod
    def soporta(cls, stack: str) -> bool:
        return cls.stacks is None or stack in cls.stacks

    def inicializado(self) -> bool:
        """La copia de trabajo ya está lista para consultar el estado (salidas, recursos)."""
        return self.trabajo.exists()

    # ---------- operaciones ----------
    @abstractmethod
    def preparar(self, variables: dict) -> None:
        """Copia la plantilla, configura el backend de estado e inicializa."""

    @abstractmethod
    def aplicar(self, progreso: Progreso) -> dict:
        """Crea o actualiza los recursos hasta el estado deseado (idempotente). Devuelve un resumen."""

    @abstractmethod
    def refrescar(self) -> None:
        """Actualiza el estado y las salidas con la realidad (p. ej. IP públicas tras reanudar)."""

    @abstractmethod
    def destruir(self, progreso: Progreso) -> dict:
        """Elimina todos los recursos del stack. Devuelve un resumen."""

    @abstractmethod
    def recursos(self) -> list[str]:
        """Direcciones de los recursos administrados (para la vista previa de eliminación)."""

    @abstractmethod
    def salidas(self) -> dict:
        """Salidas del stack, incluidas las sensibles (el llamador debe protegerlas)."""

    @abstractmethod
    def versiones(self) -> dict:
        """Versión del motor y de los proveedores usados."""


class MotorNoDisponible(Motor):
    disponible = False

    def _no(self, *_a, **_k):
        raise NotImplementedError(f"{self.etiqueta} todavía no está implementado")

    preparar = aplicar = refrescar = destruir = recursos = salidas = versiones = _no

import os
import sys
import tempfile
from pathlib import Path

# Base de datos y carpetas de datos aisladas para las pruebas
os.environ["PLATAFORMA_DATOS"] = tempfile.mkdtemp(prefix="plataforma-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from nucleo import config, db  # noqa: E402


@pytest.fixture(autouse=True)
def base_limpia():
    if config.DB_PATH.exists():
        config.DB_PATH.unlink()
    db.inicializar()
    yield

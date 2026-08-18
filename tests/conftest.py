"""Fixtures compartidas por todo el suite. IMPORTANTE: la manipulacion de DATABASE_URL de
abajo debe correr ANTES de que cualquier modulo de `app.*` se importe (pydantic-settings
lee el valor una sola vez, al importar app.core.config) - por eso este bloque va antes de
cualquier `from app...` en este archivo, y por eso ningun test module debe importar `app.*`
a nivel de modulo antes de que conftest.py se haya cargado (pytest garantiza esto: carga
conftest.py de un directorio antes de sus test_*.py).

Usa una base de datos Postgres real y separada (TEST_DATABASE_URL, o DATABASE_URL con el
nombre de la BD cambiado a `sicarx_test` si no se define) - nunca la BD de desarrollo/
produccion. Cada test corre dentro de una transaccion que se revierte al final (real SQL/
constraints, sin necesidad de recrear el schema entre tests)."""
import os
import sys

# Garantiza que la raiz del repo este en sys.path para `import app.*` sin importar como
# se invoque pytest (`pytest` vs `python -m pytest`, distinto cwd, etc.).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

_test_db_url = os.environ.get("TEST_DATABASE_URL")
if not _test_db_url:
    _base_url = os.environ.get("DATABASE_URL", "")
    if "/" in _base_url:
        _test_db_url = _base_url.rsplit("/", 1)[0] + "/sicarx_test"
if not _test_db_url:
    raise RuntimeError(
        "No se pudo determinar la URL de la base de datos de pruebas - define "
        "TEST_DATABASE_URL o DATABASE_URL en el entorno/.env."
    )
os.environ["DATABASE_URL"] = _test_db_url

import asyncio
from decimal import Decimal

import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.database import Base
from app.models.product import Product


def _run_migrations() -> None:
    """`alembic upgrade head` sincrono contra TEST_DATABASE_URL - alembic/env.py no es
    async-first en su config loading, mas simple invocarlo via su Config normal (usa su
    propio engine async internamente para las migraciones en si, ver alembic/env.py)."""
    alembic_cfg = Config(os.path.join(os.path.dirname(os.path.dirname(__file__)), "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", _test_db_url)
    command.upgrade(alembic_cfg, "head")


@pytest.fixture(scope="session", autouse=True)
def _migrated_test_db():
    """Corre las migraciones una sola vez por sesion de pytest, contra la BD de pruebas."""
    _run_migrations()
    yield


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    """Una sesion por test, atada a una transaccion externa que siempre se revierte al
    final - ningun test deja residuos para el siguiente, sin necesidad de TRUNCATE."""
    engine = create_async_engine(_test_db_url, future=True)
    connection = await engine.connect()
    trans = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")

    try:
        yield session
    finally:
        await session.close()
        await trans.rollback()
        await connection.close()
        await engine.dispose()


def make_product(**overrides) -> Product:
    """Factory con defaults razonables para un Product vendible - los tests solo
    sobreescriben los campos que les importan (stock, price, etc.)."""
    import uuid as uuid_lib

    defaults = dict(
        sicar_uuid=str(uuid_lib.uuid4()),
        sku=f"SKU-{uuid_lib.uuid4().hex[:8]}",
        name="Producto de prueba",
        price=Decimal("100.00"),
        stock=Decimal("10"),
        reserved=Decimal("0"),
        is_deleted=False,
        is_active=True,
        unit_short_name="PZA",
    )
    defaults.update(overrides)
    return Product(**defaults)

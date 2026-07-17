from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession, AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# Crear el motor asíncrono
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    # pool_pre_ping: descarta conexiones muertas antes de entregarlas
    pool_pre_ping=True,
    # api y worker son dos procesos independientes, cada uno con su propio pool -- limites
    # explicitos y conservadores evitan agotar las conexiones del plan de Postgres en Railway.
    pool_size=5,
    max_overflow=5,
)

# Fábrica de sesiones asíncronas
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False
)

class Base(AsyncAttrs, DeclarativeBase):
    pass

# Dependencia para inyectar la sesión
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
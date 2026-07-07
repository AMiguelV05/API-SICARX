from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from app.core.config import settings

# Crear el motor asíncrono
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=True,
    future=True
)

# Fábrica de sesiones asíncronas
AsyncSessionLocal = async_sessionmaker(
    engine, 
    class_=AsyncSession, 
    expire_on_commit=False
)

class Base(DeclarativeBase):
    pass

# 3. Dependencia para inyectar la sesión
async def get_db():
    async with AsyncSessionLocal() as session:
        yield session
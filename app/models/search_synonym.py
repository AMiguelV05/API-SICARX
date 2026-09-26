from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, Index, func
from sqlalchemy.dialects.postgresql import JSONB
from app.core.database import Base


class SearchSynonym(Base):
    """Una entrada de sinonimos del motor de busqueda (Typesense), administrada por el staff
    via /v1/admin/search/synonyms - ver CLAUDE.md, "Busqueda con Typesense". Postgres es la
    fuente de verdad; Typesense solo guarda una copia (synonym_service empuja cada escritura
    y el worker reconcilia la tabla completa periodicamente).

    `root` poblado = sinonimo en UNA direccion (buscar `root` tambien encuentra `synonyms`,
    no al reves); `root` NULL = multi-direccional (cada palabra encuentra a las demas).
    Las palabras se guardan ya normalizadas con search_index.normalize_search_text (misma
    normalizacion que nombres de producto y consultas), que es la forma en que Typesense las
    compara - por eso `Inalámbrico` se guarda como `inalambrico`."""
    __tablename__ = "search_synonyms"
    __table_args__ = (
        Index("ix_search_synonyms_updated_at", "updated_at"),
    )

    id = Column(Integer, primary_key=True, index=True)
    # Identificador publico y, a la vez, id del sinonimo en Typesense.
    uuid = Column(String, unique=True, index=True, nullable=False)
    root = Column(String, nullable=True)
    # JSONB (lista de strings): nada consulta dentro; JSONB solo para poder comparar por
    # igualdad al detectar una entrada duplicada.
    synonyms = Column(JSONB, nullable=False)

    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())
    # Sin cascade: las filas de admin_users nunca se borran (solo se desactivan), mismo
    # razonamiento que refunds.issued_by_admin_id. NULL para las filas sembradas por la migracion.
    updated_by_admin_id = Column(Integer, ForeignKey("admin_users.id"), nullable=True, index=True)

from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from app.core.database import Base

class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    client_account_id = Column(
        Integer, ForeignKey("client_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # sha256(token en texto plano) - el token nunca se persiste, solo su hash; no hace falta
    # un hash lento (bcrypt) como en passwords: el token ya tiene 256 bits de entropia propia.
    token_hash = Column(String, unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    # NULL = todavia vigente/sin usar. Se marca (no se borra) al usarse o al ser superado por
    # una solicitud de reset mas reciente para la misma cuenta - conserva rastro de auditoria.
    used_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

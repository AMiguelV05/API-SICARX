"""One-off script to create the first `super_admin` AdminUser - NOT imported by the app,
run manually once per environment (local/Railway via `railway run`):

    python create_admin_user.py --email admin@ferreteriacharly.com --name "Angel" --password "..." [--role super_admin]

After this, further AdminUsers are created via `POST /v1/admin/admins` (super_admin only,
see CLAUDE.md, "Admin RBAC y auditoria") - this script only exists to solve the chicken-
and-egg problem of the very first login before any AdminUser exists.
"""
import argparse
import asyncio
import logging

from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.core.security import hash_password
from app.models.admin_user import AdminUser

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("create_admin_user")


async def create_admin(email: str, name: str, password: str, role: str) -> None:
    email = email.lower()
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(select(AdminUser).where(AdminUser.email == email))
        if existing:
            logger.error(f"Ya existe un AdminUser con el correo {email!r} - nada que hacer.")
            return

        admin = AdminUser(
            email=email,
            name=name,
            hashed_password=await hash_password(password),
            role=role,
        )
        session.add(admin)
        await session.commit()
        logger.info(f"AdminUser creado: {email} (role={role}). Ya puede hacer login en POST /v1/admin/auth/login.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--role", default="super_admin", choices=("super_admin", "staff"))
    args = parser.parse_args()

    asyncio.run(create_admin(args.email, args.name, args.password, args.role))


if __name__ == "__main__":
    main()

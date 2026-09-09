"""
Script independiente (no lo importa la app) para crear o rotar el rol Postgres
de solo lectura `backup_reader`, usado exclusivamente por el workflow de
GitHub Actions que hace el respaldo offsite (`pg_dump`) fuera de Railway.

Uso:
    python create_backup_role.py --database-url "postgresql://postgres:...@host:port/db"

La cadena de conexión debe ser la del rol admin/owner de la base (por ejemplo
`DATABASE_PUBLIC_URL` del plugin de Postgres en Railway). El script es
idempotente: si `backup_reader` ya existe, solo rota su contraseña en vez de
fallar.

Al terminar, imprime la cadena de conexión completa de `backup_reader` — se
debe guardar de inmediato como el secreto `BACKUP_DATABASE_URL` en GitHub
Actions; el script no la persiste en ningún archivo.
"""

import argparse
import asyncio
import secrets
from urllib.parse import urlparse, urlunparse

import asyncpg


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        required=True,
        help="Cadena de conexión admin/owner (postgresql://usuario:password@host:puerto/db)",
    )
    args = parser.parse_args()

    password = secrets.token_urlsafe(32)

    conn = await asyncpg.connect(args.database_url)
    try:
        db_name = await conn.fetchval("SELECT current_database()")
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'backup_reader'"
        )
        if exists:
            await conn.execute(
                f"ALTER ROLE backup_reader WITH LOGIN PASSWORD '{password}'"
            )
            print("Rol backup_reader ya existía — contraseña rotada.")
        else:
            await conn.execute(
                f"CREATE ROLE backup_reader WITH LOGIN PASSWORD '{password}'"
            )
            print("Rol backup_reader creado.")

        await conn.execute(f'GRANT CONNECT ON DATABASE "{db_name}" TO backup_reader')
        await conn.execute("GRANT USAGE ON SCHEMA public TO backup_reader")
        await conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO backup_reader")
        await conn.execute("GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO backup_reader")
        await conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO backup_reader"
        )
        await conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON SEQUENCES TO backup_reader"
        )
    finally:
        await conn.close()

    parsed = urlparse(args.database_url)
    netloc = f"backup_reader:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    backup_url = urlunparse(parsed._replace(netloc=netloc))

    print()
    print("Guarda esto YA como el secreto BACKUP_DATABASE_URL en GitHub Actions:")
    print(backup_url)


if __name__ == "__main__":
    asyncio.run(main())

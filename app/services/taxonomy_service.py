import httpx
import logging
from datetime import datetime, timezone, timedelta
from fastapi import HTTPException
from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from app.models.taxonomy import Department, Category, department_category
from app.services.session_service import get_or_refresh_customer_session
from app.core.sicar_headers import storefront_headers

logger = logging.getLogger(__name__)

STORE_URL = "https://api.sicarx.com/store/"
TAXONOMY_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)
STALE_AFTER = timedelta(hours=24)

TAXONOMY_QUERY = """{
    content {
        catalog {
            categories { name uuid }
            departments { name order uuid selection { uuid order } }
        }
    }
}"""

async def fetch_taxonomy_from_sicar() -> dict:
    """Obtiene departamentos y categorias desde Sicar X usando una sesion de cliente anonima."""
    session_data = await get_or_refresh_customer_session(None)
    token = session_data["token"]
    branch_id = session_data.get("branchId", 151456)

    headers = storefront_headers(token, content_type="application/graphql", branch_id=branch_id)

    async with httpx.AsyncClient(timeout=TAXONOMY_TIMEOUT) as client:
        response = await client.post(STORE_URL, content=TAXONOMY_QUERY, headers=headers)

    if response.status_code != 200:
        logger.error(f"Error obteniendo taxonomia de Sicar: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo obtener la taxonomía de Sicar X.")

    payload = response.json()
    if "errors" in payload:
        logger.error(f"Errores GraphQL obteniendo taxonomia: {payload['errors']}")
        raise HTTPException(status_code=502, detail="No se pudo obtener la taxonomía de Sicar X.")

    catalog = payload.get("data", {}).get("content", {}).get("catalog") or {}
    return {
        "departments": catalog.get("departments") or [],
        "categories": catalog.get("categories") or [],
    }

async def _sync_taxonomy(db: AsyncSession):
    """Sincroniza el cache local (departamentos, categorias y su relacion N:M) desde Sicar X."""
    data = await fetch_taxonomy_from_sicar()
    now = datetime.now(timezone.utc)
    departments = data["departments"]
    categories = data["categories"]

    if categories:
        category_values = [
            {"uuid": c["uuid"], "name": c.get("name", ""), "updated_at": now}
            for c in categories if c.get("uuid")
        ]
        stmt = insert(Category)
        update_dict = {c.name: c for c in stmt.excluded if not c.primary_key}
        stmt = stmt.on_conflict_do_update(index_elements=["uuid"], set_=update_dict)
        await db.execute(stmt, category_values)

    if departments:
        department_values = [
            {
                "uuid": d["uuid"],
                "name": d.get("name", ""),
                "sort_order": d.get("order", 0),
                "updated_at": now,
            }
            for d in departments if d.get("uuid")
        ]
        stmt = insert(Department)
        update_dict = {c.name: c for c in stmt.excluded if not c.primary_key}
        stmt = stmt.on_conflict_do_update(index_elements=["uuid"], set_=update_dict)
        await db.execute(stmt, department_values)

        link_values = [
            {
                "department_uuid": d["uuid"],
                "category_uuid": sel["uuid"],
                "sort_order": sel.get("order", 0),
            }
            for d in departments if d.get("uuid")
            for sel in (d.get("selection") or [])
            if sel.get("uuid")
        ]
        if link_values:
            stmt = insert(department_category)
            stmt = stmt.on_conflict_do_update(
                index_elements=["department_uuid", "category_uuid"],
                set_={"sort_order": stmt.excluded.sort_order},
            )
            await db.execute(stmt, link_values)

    await db.commit()
    logger.info(f"Taxonomia sincronizada: {len(departments)} departamentos, {len(categories)} categorias.")

async def get_local_taxonomy(db: AsyncSession) -> list[Department]:
    """
    Devuelve departamentos con sus categorias desde Postgres. Si el cache esta vacio (primera
    llamada) o tiene mas de 24h, se sincroniza completo con Sicar X antes de responder.
    """
    latest = await db.scalar(select(func.max(Department.updated_at)))
    needs_refresh = latest is None or (datetime.now(timezone.utc) - latest) > STALE_AFTER

    if needs_refresh:
        logger.info("Cache de taxonomia vacio o desactualizado. Sincronizando con Sicar X...")
        try:
            await _sync_taxonomy(db)
        except HTTPException:
            if latest is None:
                raise
            logger.warning("Fallo al refrescar taxonomia; sirviendo cache existente.")

    result = await db.execute(
        select(Department)
        .options(selectinload(Department.categories))
        .order_by(Department.sort_order)
    )
    return list(result.scalars().unique().all())

import json
import httpx
import logging
from app.services.sicar_auth import sicar_auth
from app.core.retry import request_with_backoff
from app.core.sicar_headers import graphql_bearer_headers
from app.core.sicar_validation import is_safe_sicar_id

logger = logging.getLogger(__name__)
GRAPHQL_URL = "https://api.sicarx.com/graph/v1/"
DETAILS_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=5.0, pool=5.0)

async def fetch_full_details_from_sicar(uuid: str) -> dict:
    if not is_safe_sicar_id(uuid):
        logger.error(f"Identificador invalido recibido para consulta de detalles: {uuid!r}")
        return {}
    safe_uuid = uuid

    graphql_query = f"""{{
        product(uuid: "{safe_uuid}") {{
            skus
            details
            tags
            salesUnitUuid
        }}
        listImages (uuid: "{safe_uuid}") {{
            url
        }}
        content {{
            units {{
                uuid
                shortName
            }}
        }}
    }}"""

    async def attempt_fetch(token: str):
        headers = graphql_bearer_headers(token)
        async with httpx.AsyncClient(timeout=DETAILS_TIMEOUT) as client:
            return await client.post(GRAPHQL_URL, content=graphql_query, headers=headers)

    try:
        async def call_with_auth():
            return await sicar_auth.request_with_retry(attempt_fetch)

        response = await request_with_backoff(call_with_auth, max_attempts=2, context=f"Sicar X product detail {safe_uuid}")

        if response.status_code != 200:
            logger.error(f"Error obteniendo detalles para UUID {safe_uuid}. Estado: {response.status_code}")
            return {}

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            logger.error(f"Respuesta 200 no-JSON obteniendo detalles para UUID {safe_uuid}: {e}")
            return {}

        if "errors" in data:
            logger.error(f"Errores GraphQL para UUID {safe_uuid}: {data['errors']}")
            return {}

        product_data = data.get("data", {}).get("product") or {}
        images_data = data.get("data", {}).get("listImages") or []
        units_data = (data.get("data", {}).get("content") or {}).get("units") or []

        sales_unit_uuid = product_data.get("salesUnitUuid")
        unit_short_name = next(
            (u.get("shortName") for u in units_data if isinstance(u, dict) and u.get("uuid") == sales_unit_uuid),
            None,
        )

        return {
            "skus": product_data.get("skus"),
            "details": product_data.get("details"),
            "tags": product_data.get("tags"),
            "sales_unit_uuid": sales_unit_uuid,
            "unit_short_name": unit_short_name,
            "additional_images": [img.get("url") for img in images_data if isinstance(img, dict) and img.get("url")]
        }

    except httpx.RequestError as e:
        logger.error(f"Error request obteniendo los detalles para UUID {safe_uuid}: {e}")
        return {}
import httpx
import logging
from app.services.sicar_auth import sicar_auth
from app.core.sicar_headers import graphql_bearer_headers
from app.core.sicar_validation import is_safe_sicar_id

logger = logging.getLogger(__name__)
GRAPHQL_URL = "https://api.sicarx.com/graph/v1/"

async def fetch_full_details_from_sicar(uuid: str) -> dict:
    """Extrae detalles completos de Sicar X usando el token de administrador (B2B)."""

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
    }}"""

    # Sub-función para empaquetar la petición HTTP
    async def attempt_fetch(token: str):
        headers = graphql_bearer_headers(token)
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(GRAPHQL_URL, content=graphql_query, headers=headers)

    try:
        response = await sicar_auth.request_with_retry(attempt_fetch)

        if response.status_code != 200:
            logger.error(f"Error obteniendo detalles para UUID {safe_uuid}. Estado: {response.status_code}")
            return {}

        data = response.json()
        if "errors" in data:
            logger.error(f"Errores GraphQL para UUID {safe_uuid}: {data['errors']}")
            return {}

        product_data = data.get("data", {}).get("product") or {}
        images_data = data.get("data", {}).get("listImages") or []

        return {
            "skus": product_data.get("skus"),
            "details": product_data.get("details"),
            "tags": product_data.get("tags"),
            "sales_unit_uuid": product_data.get("salesUnitUuid"),
            "additional_images": [img.get("url") for img in images_data if isinstance(img, dict) and img.get("url")]
        }

    except httpx.RequestError as e:
        logger.error(f"Error request obteniendo los detalles para UUID {safe_uuid}: {e}")
        return {}
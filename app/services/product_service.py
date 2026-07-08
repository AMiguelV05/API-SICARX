import httpx
import logging
from app.services.sicar_auth import sicar_auth

logger = logging.getLogger(__name__)
GRAPHQL_URL = "https://api.sicarx.com/graph/v1/"

async def fetch_full_details_from_sicar(uuid: str) -> dict:
    """Extrae detalles completos de Sicar X usando el token de administrador (B2B)."""
    
    graphql_query = f"""{{
        product(uuid: "{uuid}") {{
            skus
            details
            tags
            salesUnitUuid
        }}
        listImages (uuid: "{uuid}") {{
            url
        }}
    }}"""

    # Sub-función para empaquetar la petición HTTP
    async def attempt_fetch(token: str):
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/graphql",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(GRAPHQL_URL, content=graphql_query, headers=headers)

    try:
        # Intentamos con el token administrativo actual
        current_token = await sicar_auth.get_token()
        response = await attempt_fetch(current_token)

        # Si caducó, FastAPI va por uno nuevo a AWS Lambda
        if response.status_code == 401:
            logger.warning(f"Token B2B expirado al consultar producto {uuid}. Renovando...")
            new_token = await sicar_auth.refresh_token()
            response = await attempt_fetch(new_token)

        if response.status_code != 200:
            logger.error(f"Error obteniendo detalles para UUID {uuid}. Estado: {response.status_code}")
            return {}

        data = response.json()
        if "errors" in data:
            logger.error(f"Errores GraphQL para UUID {uuid}: {data['errors']}")
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
        logger.error(f"Error request obteniendo los detalles para UUID {uuid}: {e}")
        return {}
import httpx
import logging
import urllib.parse
import json
import base64
from app.core.sicar_headers import storefront_headers, USER_AGENT

logger = logging.getLogger(__name__)
MAIN_SITE_URL = "https://ferreteriacharly.sicarx.shop/"
CONFIG_URL = "https://ferreteriacharly.sicarx.shop/api/ecommerce/config"

def _decode_jwt_claims(token: str) -> dict:
    """Decodifica (sin verificar firma) el payload de un JWT de sesión de Sicar X."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except (IndexError, ValueError, json.JSONDecodeError):
        return {}

async def get_or_refresh_customer_session(current_token: str = None) -> dict:
    """
    Gestiona la sesión de un cliente final.
    - Si no hay token, hace scraping de la cookie tmpStore para crear una sesión nueva.
    - Si hay token, lo valida y refresca contra la API de Sicar X.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            if current_token:
                # Refresco de sesión usando el token existente
                clean_token = current_token.replace("Bearer ", "").replace("bearer ", "").strip()
                
                headers_refresh = storefront_headers(clean_token)
                
                response = await client.get(CONFIG_URL, headers=headers_refresh)
                
                if response.status_code != 200:
                    logger.error(f"El token anterior es invalido o expiro: {response.status_code} - {response.text}")
                    raise Exception("El token anterior es inválido o expiró.")
                
                logger.info("Token refrescado correctamente")
                sicar_config = response.json()
                
            else:
                # creación de nueva sesión mediante scraping de la cookie tmpStore
                headers_new = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "User-Agent": USER_AGENT
                }
                
                response = await client.get(MAIN_SITE_URL, headers=headers_new)
                tmp_store_cookie = client.cookies.get("tmpStore")
                
                if not tmp_store_cookie:
                    logger.error("Sicar X no devolvio la cookie de sesion inicial.")
                    raise Exception("Sicar X no devolvió la cookie de sesión inicial.")
                
                # Decodificamos Base64 y URL-encoded
                padded_cookie = tmp_store_cookie + "=" * ((4 - len(tmp_store_cookie) % 4) % 4)
                base64_decoded = base64.b64decode(padded_cookie).decode('utf-8')
                url_decoded = urllib.parse.unquote(base64_decoded)
                
                sicar_config = json.loads(url_decoded)

            # Extracción del JWT y otros datos relevantes
            token = sicar_config.get("payload")
            if not token:
                logger.error("No se encontro un JWT valido en la respuesta de Sicar.")
                raise Exception("No se encontró un JWT válido en la respuesta de Sicar.")

            logger.info("Token generado correctamente")
            claims = _decode_jwt_claims(token)
            return {
                "token": token,
                "priceListUuid": sicar_config.get("priceListUuid"),
                "branchId": sicar_config.get("branches", [{}])[0].get("branchId") if sicar_config.get("branches") else 151456,
                "deliveryCost": sicar_config.get("config", {}).get("deliveryWays", {}).get("homeDeliveryCost", 50),
                "contentId": claims.get("jti")
            }
            
        except httpx.RequestError as e:
            logger.error(f"Error de red con Sicar X: {str(e)}")
            raise Exception("Error de red con Sicar X.")
        except json.JSONDecodeError:
            logger.error("Error al decodificar la estructura JSON de Sicar.")
            raise Exception("Error al decodificar la estructura JSON de Sicar.")
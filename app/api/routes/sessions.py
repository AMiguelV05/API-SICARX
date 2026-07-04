from fastapi import APIRouter, HTTPException, Header
import httpx
import urllib.parse
import json
import base64

router = APIRouter(prefix="/session", tags=["Session"])

MAIN_SITE_URL = "https://ferreteriacharly.sicarx.shop/"
CONFIG_URL = "https://ferreteriacharly.sicarx.shop/api/ecommerce/config"

@router.post("/init")
async def initialize_or_refresh_session(authorization: str = Header(None)):
    """
    Si no hay token, simula una visita nueva para extraer la cookie tmpStore.
    Si hay token, lo envía al endpoint de config para refrescar la sesión existente.
    """
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            print(f"Authorization header recibido: {authorization}")
            if authorization:
                """Refresco de sesión existente"""
                # Limpiamos "Bearer " por si el frontend o Swagger lo enviaron
                clean_token = authorization.replace("Bearer ", "").replace("bearer ", "").strip()
                
                headers_refresh = {
                    "Accept": "application/json, text/plain, */*",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Origin": "https://ferreteriacharly.sicarx.shop",
                    "Referer": "https://ferreteriacharly.sicarx.shop/",
                    "Authorization": clean_token
                }
                
                response = await client.get(CONFIG_URL, headers=headers_refresh)
                
                if response.status_code != 200:
                    raise HTTPException(status_code=401, detail=f"El token anterior es inválido o no se pudo refrescar: {response.text}")
                
                sicar_config = response.json()
                print("Sesión refrescada exitosamente.")

            else:
                """Nueva sesión"""
                headers_new = {
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
                
                response = await client.get(MAIN_SITE_URL, headers=headers_new)
                
                tmp_store_cookie = client.cookies.get("tmpStore")
                if not tmp_store_cookie:
                    raise HTTPException(status_code=500, detail="Sicar X no devolvió la cookie de sesión.")
                
                # Decodificamos Base64 y URL-encoded
                padded_cookie = tmp_store_cookie + "=" * ((4 - len(tmp_store_cookie) % 4) % 4)
                base64_decoded = base64.b64decode(padded_cookie).decode('utf-8')
                url_decoded = urllib.parse.unquote(base64_decoded)
                
                sicar_config = json.loads(url_decoded)
                print("Nueva sesión extraída desde la cookie.")

            # Extraemos el token JWT y los datos vitales
            token = sicar_config.get("payload")
            
            if not token:
                raise HTTPException(status_code=500, detail="No se encontró un payload válido en la respuesta de Sicar.")

            return {
                "token": token,
                "priceListUuid": sicar_config.get("priceListUuid"),
                "branchId": sicar_config.get("branches", [{}])[0].get("branchId") if sicar_config.get("branches") else 151456,
                "deliveryCost": sicar_config.get("config", {}).get("deliveryWays", {}).get("homeDeliveryCost", 50)
            }
            
        except httpx.RequestError as e:
            raise HTTPException(status_code=503, detail=f"Error de red al intentar conectar con Sicar: {str(e)}")
        except json.JSONDecodeError:
            raise HTTPException(status_code=500, detail="Error al decodificar la estructura JSON de Sicar.")
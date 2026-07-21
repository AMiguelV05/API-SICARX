import json
import logging
from pathlib import Path

import httpx
from fastapi import HTTPException

CP_LOOKUP_URL = "https://www.correosmexico.com.mx/api/cp"
SEPOMEX_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)

_STATES_COUNTIES_PATH = Path(__file__).resolve().parent.parent / "data" / "mx_states_counties.json"
_STATES_COUNTIES: dict[str, list[str]] = json.loads(_STATES_COUNTIES_PATH.read_text(encoding="utf-8"))

logger = logging.getLogger(__name__)

async def get_zip_info(zip_code: str) -> dict | None:
    """Consulta el API publico de correosmexico.com.mx (datos de Sepomex) para un codigo
    postal. Confirmado en vivo (ver CLAUDE.md): devuelve uno o mas asentamientos (colonias)
    por CP, cada uno con estado/municipio/ciudad -- los tres coinciden siempre entre si en
    una misma respuesta, asi que se toman del primer resultado; solo `asentamiento` varia.

    Devuelve None si el CP tiene formato valido pero no existe (`total: 0`) - la ruta lo
    convierte en 404. Un error de red o una respuesta inesperada es un problema de
    validacion critico para el formulario, no un enriquecimiento best-effort, asi que se
    propaga como 502 en vez de responder None silenciosamente."""
    try:
        async with httpx.AsyncClient(timeout=SEPOMEX_TIMEOUT) as client:
            response = await client.get(CP_LOOKUP_URL, params={"cp": zip_code})
    except httpx.HTTPError as e:
        logger.error(f"Error de red consultando codigo postal {zip_code} en correosmexico.com.mx: {e}")
        raise HTTPException(status_code=502, detail="No se pudo consultar el código postal. Intenta nuevamente.")

    if response.status_code != 200:
        logger.error(f"correosmexico.com.mx rechazo la consulta del CP {zip_code}: {response.status_code} - {response.text}")
        raise HTTPException(status_code=502, detail="No se pudo consultar el código postal. Intenta nuevamente.")

    data = response.json()
    resultados = data.get("resultados") or []
    if not resultados:
        return None

    first = resultados[0]
    colonias = list(dict.fromkeys(r["asentamiento"] for r in resultados if r.get("asentamiento")))

    return {
        "zip_code": zip_code,
        "state": first.get("estado"),
        "city": first.get("ciudad"),
        "county": first.get("municipio"),
        "colonias": colonias,
    }

def get_states() -> list[str]:
    """Catalogo estatico de los 32 estados (INEGI) - ver CLAUDE.md sobre por que esto no
    se consulta en vivo: correosmexico.com.mx no expone un endpoint de "listar todo", solo
    busqueda por CP o texto libre limitada a 25 resultados."""
    return sorted(_STATES_COUNTIES.keys())

def get_counties(state: str) -> list[str] | None:
    """Catalogo estatico de municipios por estado. None si `state` no coincide
    exactamente con una clave del catalogo (la ruta lo convierte en 404) - los nombres
    siguen el mismo formato oficial INEGI que devuelve `estado` en get_zip_info (p. ej.
    "Michoacán de Ocampo", "Coahuila de Zaragoza"), confirmado en vivo para que ambos
    flujos (autocompletado por CP y seleccion manual de estado) usen el mismo vocabulario."""
    return _STATES_COUNTIES.get(state)

"""One-off script to seed `vehicles` from Gonher's public vehicle-fitment reference API
(catalogo.grupogonher.com) - a generic make/model/year/engine reference for the admin's
vehicle picker, NOT linked to any real product (near-zero SKU overlap with this store's
SICAR catalog). NOT imported by the app - run manually, once:

    python import_gonher_vehicles.py

Only seeds `vehicles`, never `product_vehicles` (that stays a human/admin task via
PUT /v1/admin/vehicles/{uuid}/products).

Scope: only Gonher's "Automotriz" (tipo 1) and "Motocicletas" (tipo 3) - "Camiones..."
(tipo 2) is almost entirely heavy industrial equipment, irrelevant to this catalog.

Approach: Gonher's cascading dropdown endpoints don't actually narrow by make (same
generic 1948-2026 year range regardless), so this fetches the one bulk endpoint
(Articulos/GetTagsVehiculos, ~94k flat "<year> <make> <model...> <engine>" strings) plus
the two make lists (Armadoras/GetArmadorasByTipo/{1,3}), and parses tags locally instead
of crawling tens of thousands of dropdown requests for no better data.

HEURISTIC parse of third-party free-text data, not guaranteed 100% clean - spot-check a
sample after running. Two engine formats appear: car-style ("L4 1.6L", cylinder-config +
displacement with an L suffix, optional trailing modifier like "Turbo") and moto/ATV-style
(a bare integer cc count, e.g. "249"). Five makes appear in BOTH lists (Honda, Bmw,
Triumph, Peugeot, Suzuki) - for those, the matched engine format breaks the tie
(car-style -> AUTOMOTIVE, bare-cc -> MOTORCYCLE).
"""
import asyncio
import logging
import re
import uuid as uuid_lib
from datetime import datetime, timezone

import httpx
from sqlalchemy import select, insert

from app.core.database import AsyncSessionLocal
from app.models.vehicle import Vehicle

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("import_gonher_vehicles")
logging.getLogger("httpx").setLevel(logging.WARNING)

API_BASE = "https://catalogo.grupogonher.com/CatalogoGonherServices/api"
TIPO_AUTOMOTIVE = 1
TIPO_MOTORCYCLE = 3

YEAR_RE = re.compile(r"^(19|20)\d{2}$")
CYLINDER_RE = re.compile(r"^[A-Za-z]{1,2}\d{1,2}$")
DISPLACEMENT_RE = re.compile(r"^\d+(\.\d+)?L$", re.IGNORECASE)
CC_RE = re.compile(r"^\d{2,4}$")


async def _fetch_makes(client: httpx.AsyncClient, tipo: int) -> set[str]:
    resp = await client.get(f"{API_BASE}/Armadoras/GetArmadorasByTipo/{tipo}")
    resp.raise_for_status()
    return {row["armadora"] for row in resp.json()}


async def _fetch_tags(client: httpx.AsyncClient) -> list[str]:
    resp = await client.get(f"{API_BASE}/Articulos/GetTagsVehiculos/")
    resp.raise_for_status()
    return resp.json()


def _parse_tag(tag: str, make_tokens: list[tuple[str, list[str]]], ambiguous_makes: set[str], motorcycle_makes: set[str]) -> tuple[str, str, str, int, str | None] | None:
    """Devuelve (vehicle_type, make, model, year, engine) o None si `tag` no se puede
    interpretar de forma confiable (sin anio inicial valido, sin make conocido, o sin
    texto de modelo despues de quitar make/engine)."""
    tokens = tag.split()
    if not tokens or not YEAR_RE.match(tokens[0]):
        return None
    year = int(tokens[0])
    rest = tokens[1:]

    matched_make: str | None = None
    remaining: list[str] | None = None
    for name, name_tokens in make_tokens:
        n = len(name_tokens)
        if len(rest) >= n and [t.lower() for t in rest[:n]] == [t.lower() for t in name_tokens]:
            matched_make = name
            remaining = rest[n:]
            break
    if not matched_make or not remaining:
        return None

    if remaining[-1].upper() == "N/A":
        remaining = remaining[:-1]
    if not remaining:
        return None

    engine: str | None = None
    engine_format: str | None = None
    model_tokens = remaining
    # formato "carro": cilindros + desplazamiento con sufijo L, buscado desde el final.
    for i in range(len(remaining) - 2, -1, -1):
        if CYLINDER_RE.match(remaining[i]) and DISPLACEMENT_RE.match(remaining[i + 1]):
            engine = " ".join([remaining[i], remaining[i + 1]] + remaining[i + 2:])
            model_tokens = remaining[:i]
            engine_format = "car"
            break
    # formato "moto/atv": un entero suelto de cc al final, sin sufijo.
    if engine is None and CC_RE.match(remaining[-1]):
        engine = f"{remaining[-1]}cc"
        model_tokens = remaining[:-1]
        engine_format = "moto"

    if not model_tokens:
        return None
    model = " ".join(model_tokens)

    if matched_make in ambiguous_makes:
        vehicle_type = "MOTORCYCLE" if engine_format == "moto" else "AUTOMOTIVE"
    elif matched_make in motorcycle_makes:
        vehicle_type = "MOTORCYCLE"
    else:
        vehicle_type = "AUTOMOTIVE"

    return (vehicle_type, matched_make, model, year, engine)


async def main() -> None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        automotive_makes = await _fetch_makes(client, TIPO_AUTOMOTIVE)
        motorcycle_makes = await _fetch_makes(client, TIPO_MOTORCYCLE)
        tags = await _fetch_tags(client)
    logger.info(f"Gonher: {len(automotive_makes)} makes Automotriz, {len(motorcycle_makes)} makes Motocicletas, {len(tags)} tags totales.")

    ambiguous_makes = automotive_makes & motorcycle_makes
    all_makes = sorted(automotive_makes | motorcycle_makes, key=lambda m: (-len(m.split()), -len(m)))
    make_tokens = [(m, m.split()) for m in all_makes]

    parsed: set[tuple[str, str, str, int, str | None]] = set()
    skipped = 0
    for tag in tags:
        row = _parse_tag(tag, make_tokens, ambiguous_makes, motorcycle_makes)
        if row is None:
            skipped += 1
        else:
            parsed.add(row)
    logger.info(f"Parseo: {len(parsed)} combinaciones unicas reconocidas (Automotriz+Motocicletas), {skipped} tags omitidos (sin anio valido, make no reconocido/fuera de alcance, o sin texto de modelo).")

    async with AsyncSessionLocal() as db:
        existing_result = await db.execute(select(Vehicle.vehicle_type, Vehicle.make, Vehicle.model, Vehicle.year_start, Vehicle.engine))
        existing = {(row.vehicle_type, row.make, row.model, row.year_start, row.engine) for row in existing_result.all()}

        to_insert = [row for row in parsed if row not in existing]
        logger.info(f"{len(parsed) - len(to_insert)} ya existian en `vehicles`, {len(to_insert)} filas nuevas por insertar.")

        # Insert masivo en lotes, no create_vehicle fila por fila (~40k transacciones si no).
        now = datetime.now(timezone.utc)
        batch_size = 1000
        inserted = 0
        for i in range(0, len(to_insert), batch_size):
            batch = to_insert[i:i + batch_size]
            rows = [
                {
                    "uuid": str(uuid_lib.uuid4()),
                    "vehicle_type": vehicle_type,
                    "make": make,
                    "model": model,
                    "year_start": year,
                    "year_end": year,
                    "engine": engine,
                    "updated_at": now,
                }
                for vehicle_type, make, model, year, engine in batch
            ]
            await db.execute(insert(Vehicle), rows)
            await db.commit()
            inserted += len(rows)
            logger.info(f"{inserted}/{len(to_insert)} insertadas...")

    logger.info(f"Listo: {inserted} vehiculos insertados. Revisa una muestra manualmente antes de confiar en los datos - ver el docstring de este script.")


if __name__ == "__main__":
    asyncio.run(main())

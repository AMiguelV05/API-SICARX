"""Sondeo de solo lectura del webservice de URREA (Fase 0 de la integracion). Script suelto,
no lo importa la app. Llama a las opciones 1, 2 y 4 con las credenciales de URREA_USER /
URREA_PASSWORD (.env), guarda las respuestas crudas en OUT_DIR y resume lo necesario para
cerrar el diseno: tamano/latencia de la masiva, nombres reales de campos, valores de
estatusInventario, forma de `caracteristica`, formato de errores y cobertura del
emparejamiento "U"+codigo / codigo de barras contra el catalogo local.

    python urrea_probe.py OUT_DIR
    python urrea_probe.py OUT_DIR --codes 10CAP 000AA --qty 30
    python urrea_probe.py OUT_DIR --skip-bulk
    python urrea_probe.py OUT_DIR --match          # cobertura contra DATABASE_URL (solo lectura)

OUT_DIR debe quedar FUERA del repo: las respuestas incluyen nuestro precio de costo. El body
de las peticiones (lleva la contrasena) nunca se guarda ni se imprime.
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections import Counter

import httpx

from app.core.config import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("urrea_probe")

TIMEOUT = httpx.Timeout(10.0, read=300.0)


def _post(client: httpx.Client, body: dict, label: str, out_dir: str) -> tuple[int, object]:
    """POST al webservice; guarda la respuesta cruda en OUT_DIR/<label>.json (o .txt si no es JSON)."""
    started = time.monotonic()
    response = client.post(settings.URREA_WS_URL, json=body)
    elapsed = time.monotonic() - started
    raw = response.content
    logger.info(
        f"[{label}] HTTP {response.status_code} en {elapsed:.2f} s, {len(raw) / 1024:.1f} KiB, "
        f"content-type={response.headers.get('content-type')!r}"
    )
    try:
        data = response.json()
        path = os.path.join(out_dir, f"{label}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except ValueError:
        data = None
        path = os.path.join(out_dir, f"{label}.txt")
        with open(path, "wb") as fh:
            fh.write(raw)
        logger.warning(f"[{label}] La respuesta no es JSON; primeros 300 bytes: {raw[:300]!r}")
    return response.status_code, data


def _rows(data, preferred_key: str) -> list[dict]:
    """Lista de filas de la respuesta, tolerando que la clave venga con otro casing."""
    if not isinstance(data, dict):
        return []
    for key, value in data.items():
        if key.lower() == preferred_key.lower() and isinstance(value, list):
            return [r for r in value if isinstance(r, dict)]
    # Respaldo: la primera lista de dicts que aparezca.
    for value in data.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return value
    return []


def _summarize(label: str, data, rows_key: str) -> list[dict]:
    if isinstance(data, dict):
        logger.info(f"[{label}] claves de primer nivel: {list(data.keys())}; status={data.get('status')!r}")
    rows = _rows(data, rows_key)
    logger.info(f"[{label}] filas: {len(rows)}")
    if not rows:
        return rows
    key_counts = Counter(k for r in rows for k in r.keys())
    logger.info(f"[{label}] claves de fila (conteo): {dict(key_counts)}")
    status_key = next((k for k in key_counts if k.lower() == "estatusinventario"), None)
    if status_key:
        logger.info(f"[{label}] valores de {status_key}: {dict(Counter(str(r.get(status_key)) for r in rows))}")
    char_key = next((k for k in key_counts if k.lower() == "caracteristica"), None)
    if char_key:
        types = Counter(type(r.get(char_key)).__name__ for r in rows)
        sample = next((r.get(char_key) for r in rows if r.get(char_key) not in ("", None, [])), None)
        logger.info(f"[{label}] tipos de {char_key}: {dict(types)}; ejemplo no vacio: {json.dumps(sample, ensure_ascii=False)[:500]}")
    for numeric in ("precio", "multiplo", "alto", "fondo", "ancho", "peso"):
        key = next((k for k in key_counts if k.lower() == numeric), None)
        if key:
            types = Counter(type(r.get(key)).__name__ for r in rows)
            logger.info(f"[{label}] tipos de {key}: {dict(types)}")
    currency_key = next((k for k in key_counts if k.lower() == "moneda"), None)
    if currency_key:
        logger.info(f"[{label}] monedas: {dict(Counter(str(r.get(currency_key)) for r in rows))}")
    return rows


async def _coverage(rows: list[dict], prefix: str, out_dir: str) -> None:
    """Cuantos codigos de la masiva resuelven a un producto local, con la misma regla que
    import_package_dimensions.py ("U"+codigo y luego codigo de barras). Solo lectura."""
    from app.core.database import AsyncSessionLocal, engine
    from app.services.bulk_import_service import _resolve_products

    def get(row: dict, name: str):
        return next((v for k, v in row.items() if k.lower() == name), None)

    codes = [(str(get(r, "codigo") or "").strip(), str(get(r, "codigobarras") or "").strip() or None) for r in rows]
    codes = [(c, b) for c, b in codes if c]
    try:
        async with AsyncSessionLocal() as db:
            by_sku = await _resolve_products(db, {f"{prefix}{c}" for c, _ in codes})
            pending = [(c, b) for c, b in codes if f"{prefix}{c}".upper() not in by_sku]
            by_barcode = await _resolve_products(db, {b for _, b in pending if b})
            await db.rollback()
    finally:
        await engine.dispose()

    unmatched = [c for c, b in pending if not (b and b.upper() in by_barcode)]
    matched_sku = len(codes) - len(pending)
    matched_barcode = len(pending) - len(unmatched)
    logger.info(
        f"Cobertura: {len(codes)} codigos URREA; por {prefix}+codigo: {matched_sku}; "
        f"por codigo de barras: {matched_barcode}; sin producto local: {len(unmatched)}."
    )
    product_ids = list(by_sku.values()) + list(by_barcode.values())
    dupes = [pid for pid, n in Counter(product_ids).items() if n > 1]
    if dupes:
        logger.warning(f"{len(dupes)} producto(s) local(es) emparejan con mas de un codigo URREA.")
    with open(os.path.join(out_dir, "sin_emparejar.txt"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(unmatched))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sondeo de solo lectura del webservice de URREA.")
    parser.add_argument("out_dir", help="Directorio de salida (fuera del repo)")
    parser.add_argument("--codes", nargs="+", default=["10CAP", "000AA"], help="Codigos URREA para opciones 1 y 2")
    parser.add_argument("--qty", type=int, default=1, help="Cantidad para la opcion 2 (default 1)")
    parser.add_argument("--skip-bulk", action="store_true", help="No llamar a la opcion 4 (masiva)")
    parser.add_argument("--batch", type=int, default=0, help="Si > 0, prueba la opcion 1 con N codigos tomados de la masiva")
    parser.add_argument("--match", action="store_true", help="Medir cobertura del emparejamiento contra DATABASE_URL")
    args = parser.parse_args()

    if not (settings.URREA_USER and settings.URREA_PASSWORD):
        sys.exit("Faltan URREA_USER / URREA_PASSWORD en el .env.")
    if os.path.abspath(args.out_dir).startswith(os.path.abspath(os.path.dirname(__file__))):
        sys.exit("OUT_DIR debe quedar fuera del repo (las respuestas traen precios de costo).")
    os.makedirs(args.out_dir, exist_ok=True)

    auth = {"usuario": settings.URREA_USER, "password": settings.URREA_PASSWORD}
    with httpx.Client(timeout=TIMEOUT) as client:
        # Errores: credenciales invalidas y codigo inexistente.
        _post(client, {"opcion": 2, "usuario": settings.URREA_USER, "password": "x-invalida",
                       "producto": {"codigoProducto": args.codes[0], "cantidad": str(args.qty)}}, "error_401", args.out_dir)
        _post(client, {"opcion": 2, **auth, "producto": {"codigoProducto": "ZZZ-NO-EXISTE", "cantidad": "1"}}, "error_codigo", args.out_dir)

        # Opcion 2: un codigo, cantidad como string (ejemplo del PDF) y como entero.
        for code in args.codes:
            _, data = _post(client, {"opcion": 2, **auth, "producto": {"codigoProducto": code, "cantidad": str(args.qty)}}, f"opcion2_{code}", args.out_dir)
            _summarize(f"opcion2_{code}", data, "disponibilidad")
        _, data = _post(client, {"opcion": 2, **auth, "producto": {"codigoProducto": args.codes[0], "cantidad": args.qty}}, "opcion2_cantidad_int", args.out_dir)
        _summarize("opcion2_cantidad_int", data, "disponibilidad")
        _, data = _post(client, {"opcion": 2, **auth, "producto": {"codigoProducto": args.codes[0], "cantidad": "999999"}}, "opcion2_cantidad_enorme", args.out_dir)
        _summarize("opcion2_cantidad_enorme", data, "disponibilidad")

        # Opcion 1: informacion de producto.
        _, data = _post(client, {"opcion": 1, **auth, "codigoProducto": args.codes}, "opcion1", args.out_dir)
        _summarize("opcion1", data, "informacionProducto")

        bulk_rows: list[dict] = []
        if not args.skip_bulk:
            _, data = _post(client, {"opcion": 4, **auth}, "opcion4", args.out_dir)
            bulk_rows = _summarize("opcion4", data, "resultadoDispMasiva")

        if args.batch and bulk_rows:
            sample = [str(next((v for k, v in r.items() if k.lower() == "codigo"), "")) for r in bulk_rows[: args.batch]]
            _, data = _post(client, {"opcion": 1, **auth, "codigoProducto": sample}, f"opcion1_lote_{args.batch}", args.out_dir)
            rows = _summarize(f"opcion1_lote_{args.batch}", data, "informacionProducto")
            logger.info(f"Lote de {args.batch} codigos -> {len(rows)} filas devueltas.")

    if args.match and bulk_rows:
        asyncio.run(_coverage(bulk_rows, settings.URREA_SKU_PREFIX, args.out_dir))
    logger.info(f"Respuestas guardadas en {os.path.abspath(args.out_dir)}")


if __name__ == "__main__":
    main()

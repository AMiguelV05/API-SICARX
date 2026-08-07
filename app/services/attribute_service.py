import json
import logging
import re
import unicodedata
import uuid as uuid_lib
from datetime import datetime, timezone
from fastapi import HTTPException, status
from sqlalchemy import select, func, delete, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.attribute import Attribute, AttributePreset, attribute_preset_items, VariantGroup
from app.models.product import Product

logger = logging.getLogger(__name__)


# --- Slugs: mismo slugify que taxonomy_service, generalizado para Attribute/AttributePreset -----

def _slugify(name: str, fallback: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_name.lower()).strip("-")
    return slug or fallback


async def _unique_slug(db: AsyncSession, model, name: str, *, fallback: str, exclude_uuid: str | None = None) -> str:
    base = _slugify(name, fallback)
    query = select(model.slug).where(model.slug.like(f"{base}%"))
    if exclude_uuid:
        query = query.where(model.uuid != exclude_uuid)
    existing = {row[0] for row in (await db.execute(query)).all()}

    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def _escape_ilike(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --- Attributes: catalogo de definiciones -----

def _validate_enum_values(data_type: str, allowed_values: list[str] | None) -> None:
    if data_type == "ENUM" and (not allowed_values or len(allowed_values) < 2):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="allowedValues es requerido (minimo 2 valores) cuando dataType es ENUM.")


async def _attribute_in_use(db: AsyncSession, slug: str) -> bool:
    """True si algun producto ACTIVO (is_deleted=False) tiene esta clave en su
    Product.attributes (JSONB) - condiciona si renombrar/cambiar el tipo del atributo es
    seguro, y si se puede borrar. Filtra is_deleted igual que get_attributes_for_product/
    list_variant_group_products, para que un producto discontinuado por el sync (ya
    invisible en cualquier listado admin) no bloquee para siempre un renombre/borrado de
    algo que ya no se ve en ningun lado."""
    return bool(await db.scalar(select(Product.id).where(Product.attributes.has_key(slug), Product.is_deleted == False).limit(1)))


async def create_attribute(db: AsyncSession, name: str, data_type: str, allowed_values: list[str] | None, unit: str | None) -> Attribute:
    _validate_enum_values(data_type, allowed_values)

    attribute = Attribute(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        slug=await _unique_slug(db, Attribute, name, fallback="atributo"),
        data_type=data_type,
        allowed_values=allowed_values if data_type == "ENUM" else None,
        unit=unit,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(attribute)
    await db.commit()
    await db.refresh(attribute)
    logger.info(f"Atributo '{attribute.name}' ({attribute.uuid}) creado via /admin.")
    return attribute


async def update_attribute(db: AsyncSession, attribute_uuid: str, data: dict) -> Attribute:
    """`data` = AttributeUpdateRequest.model_dump(exclude_unset=True). Renombrar (cambia el
    slug) o cambiar dataType mientras el atributo ya tiene valores guardados en productos
    se bloquea (409) - ambos romperian los valores existentes en Product.attributes."""
    attribute = await db.get(Attribute, attribute_uuid)
    if attribute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atributo no encontrado.")

    effective_data_type = data.get("data_type", attribute.data_type)
    effective_allowed_values = data["allowed_values"] if "allowed_values" in data else attribute.allowed_values
    _validate_enum_values(effective_data_type, effective_allowed_values)

    new_slug = attribute.slug
    if "name" in data:
        new_name = data["name"]
        if not new_name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name no puede estar vacio.")
        new_slug = await _unique_slug(db, Attribute, new_name, fallback="atributo", exclude_uuid=attribute_uuid)

    changing_type = "data_type" in data and data["data_type"] != attribute.data_type
    changing_slug = new_slug != attribute.slug
    if (changing_type or changing_slug) and await _attribute_in_use(db, attribute.slug):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Este atributo ya tiene valores guardados en productos; renombrarlo o cambiar su tipo de dato los romperia. Quita los valores existentes primero o crea un atributo nuevo.",
        )

    if "name" in data:
        attribute.name = data["name"]
        attribute.slug = new_slug
    if "data_type" in data:
        attribute.data_type = data["data_type"]
    if "allowed_values" in data:
        attribute.allowed_values = data["allowed_values"] if effective_data_type == "ENUM" else None
    if "unit" in data:
        attribute.unit = data["unit"]

    attribute.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(attribute)
    logger.info(f"Atributo {attribute.uuid} actualizado via /admin.")
    return attribute


async def delete_attribute(db: AsyncSession, attribute_uuid: str) -> None:
    attribute = await db.get(Attribute, attribute_uuid)
    if attribute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atributo no encontrado.")

    if await _attribute_in_use(db, attribute.slug):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este atributo tiene valores guardados en productos; quitalos primero.")

    has_preset_membership = await db.scalar(select(attribute_preset_items.c.preset_uuid).where(attribute_preset_items.c.attribute_uuid == attribute_uuid).limit(1))
    if has_preset_membership:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este atributo esta asignado a uno o mas presets; quitalo primero.")

    await db.delete(attribute)
    await db.commit()
    logger.info(f"Atributo {attribute_uuid} eliminado via /admin.")


async def list_attributes(db: AsyncSession, *, search: str | None, data_type: str | None, limit: int, offset: int) -> tuple[int, list[Attribute]]:
    stmt = select(Attribute)
    if search:
        stmt = stmt.where(Attribute.name.ilike(f"%{_escape_ilike(search)}%", escape="\\"))
    if data_type:
        stmt = stmt.where(Attribute.data_type == data_type)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.order_by(func.lower(Attribute.name)).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def list_products_with_attribute(db: AsyncSession, attribute_uuid: str, limit: int, offset: int) -> tuple[int, Attribute, list[Product]]:
    """Direccion inversa de get_product_attributes: dado un atributo, que productos tienen
    esta clave guardada en su `attributes` (JSONB) - mismo proposito que
    list_category_products/list_vehicle_products, pero via containment JSONB (`?`) en vez
    de una tabla pivote. Filtra is_deleted igual que esos dos (mismo criterio de
    _attribute_in_use). Devuelve tambien `attribute` (no solo su uuid) para que el caller
    pueda leer `product.attributes[attribute.slug]` sin una segunda consulta."""
    attribute = await db.get(Attribute, attribute_uuid)
    if attribute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atributo no encontrado.")

    base = select(Product).where(Product.attributes.has_key(attribute.slug), Product.is_deleted == False)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(Product.name).limit(limit).offset(offset))
    return total or 0, attribute, list(result.scalars().all())


async def replace_attribute_products(db: AsyncSession, attribute_uuid: str, values: list[dict]) -> list[dict]:
    """Direccion attribute-primero: reemplaza el conjunto COMPLETO de productos que tienen
    ESTE atributo asignado (complementa `replace_product_attributes`, product-primero, que
    reemplaza TODOS los atributos de un producto). Solo TOCA la clave de este atributo en
    el `attributes` JSONB de cada producto - un producto que ya la tenia y no viene en
    `values` la pierde, pero cualquier otro atributo que ya tuviera guardado no se toca."""
    attribute = await db.get(Attribute, attribute_uuid)
    if attribute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atributo no encontrado.")

    unique_uuids = sorted({v["product_uuid"] for v in values})
    target_products: dict[str, Product] = {}
    if unique_uuids:
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False))
        target_products = {p.sicar_uuid: p for p in result.scalars().all()}
        missing = [u for u in unique_uuids if u not in target_products]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")

    value_by_uuid: dict[str, object] = {}
    errors: list[str] = []
    for v in values:
        try:
            value_by_uuid[v["product_uuid"]] = coerce_and_validate_value(attribute, v["value"])
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(errors))

    current_result = await db.execute(select(Product).where(Product.attributes.has_key(attribute.slug), Product.is_deleted == False))
    current_products = {p.sicar_uuid: p for p in current_result.scalars().all()}

    # Quita la clave de quien ya la tenia y no esta en el nuevo conjunto - reasignacion
    # completa (no dict.pop in-place: SQLAlchemy solo detecta el cambio si se reasigna el
    # atributo a un objeto nuevo, mismo criterio que replace_product_attributes).
    for sicar_uuid, product in current_products.items():
        if sicar_uuid not in target_products:
            new_attrs = dict(product.attributes or {})
            new_attrs.pop(attribute.slug, None)
            product.attributes = new_attrs or None

    for sicar_uuid, product in target_products.items():
        new_attrs = dict(product.attributes or {})
        new_attrs[attribute.slug] = value_by_uuid[sicar_uuid]
        product.attributes = new_attrs

    await db.commit()
    removed_count = len(current_products.keys() - target_products.keys())
    logger.info(
        f"Atributo {attribute_uuid}: productos reemplazados via /admin "
        f"({len(target_products)} con valor asignado, {removed_count} removidos)."
    )
    return [{"product_uuid": u, "value": value_by_uuid[u]} for u in unique_uuids]


async def patch_attribute_products(db: AsyncSession, attribute_uuid: str, upsert_values: list[dict], remove_uuids: list[str]) -> tuple[list[dict], list[str], int, int]:
    """Incremental (a diferencia de replace_attribute_products): asigna/actualiza el valor
    de este atributo en un lote de productos o le quita la clave a otro lote, sin tocar el
    resto de productos que ya tenian este atributo asignado - pensado para atributos con
    mas productos asignados que el limite de GET .../products. `remove_uuids` es tolerante
    (un uuid que no exista o no tenga la clave se ignora)."""
    attribute = await db.get(Attribute, attribute_uuid)
    if attribute is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Atributo no encontrado.")

    unique_uuids = sorted({v["product_uuid"] for v in upsert_values})
    upsert_products: dict[str, Product] = {}
    if unique_uuids:
        result = await db.execute(select(Product).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False))
        upsert_products = {p.sicar_uuid: p for p in result.scalars().all()}
        missing = [u for u in unique_uuids if u not in upsert_products]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")

    value_by_uuid: dict[str, object] = {}
    errors: list[str] = []
    for v in upsert_values:
        try:
            value_by_uuid[v["product_uuid"]] = coerce_and_validate_value(attribute, v["value"])
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(errors))

    for sicar_uuid, product in upsert_products.items():
        new_attrs = dict(product.attributes or {})
        new_attrs[attribute.slug] = value_by_uuid[sicar_uuid]
        product.attributes = new_attrs

    unique_remove = sorted(set(remove_uuids))
    removed_uuids: list[str] = []
    if unique_remove:
        current_result = await db.execute(
            select(Product).where(
                Product.attributes.has_key(attribute.slug),
                Product.sicar_uuid.in_(unique_remove),
                Product.is_deleted == False,
            )
        )
        for product in current_result.scalars().all():
            new_attrs = dict(product.attributes or {})
            new_attrs.pop(attribute.slug, None)
            product.attributes = new_attrs or None
            removed_uuids.append(product.sicar_uuid)

    await db.commit()
    logger.info(
        f"Atributo {attribute_uuid}: {len(upsert_products)} producto(s) actualizado(s), "
        f"{len(removed_uuids)} quitado(s) via PATCH /admin."
    )
    return [{"product_uuid": u, "value": value_by_uuid[u]} for u in unique_uuids], sorted(removed_uuids), len(upsert_products), len(removed_uuids)


# --- Attribute presets: bundles de conveniencia, nunca obligatorios ni validados contra un producto -----

async def create_preset(db: AsyncSession, name: str) -> AttributePreset:
    preset = AttributePreset(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        slug=await _unique_slug(db, AttributePreset, name, fallback="preset"),
        updated_at=datetime.now(timezone.utc),
    )
    db.add(preset)
    await db.commit()
    await db.refresh(preset)
    logger.info(f"Preset de atributos '{preset.name}' ({preset.uuid}) creado via /admin.")
    return preset


async def update_preset(db: AsyncSession, preset_uuid: str, data: dict) -> AttributePreset:
    preset = await db.get(AttributePreset, preset_uuid)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preset no encontrado.")

    if "name" in data:
        new_name = data["name"]
        if not new_name:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name no puede estar vacio.")
        preset.name = new_name
        preset.slug = await _unique_slug(db, AttributePreset, new_name, fallback="preset", exclude_uuid=preset_uuid)

    preset.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(preset)
    logger.info(f"Preset de atributos {preset.uuid} actualizado via /admin.")
    return preset


async def delete_preset(db: AsyncSession, preset_uuid: str) -> None:
    """A diferencia de delete_attribute/delete_variant_group, borrar un preset nunca se
    bloquea - no gatea ni valida nada de un producto, solo es un atajo de captura. Sus
    propias filas de attribute_preset_items se limpian aqui explicitamente (sin
    ondelete=CASCADE en la FK) antes de borrar el preset."""
    preset = await db.get(AttributePreset, preset_uuid)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preset no encontrado.")

    await db.execute(delete(attribute_preset_items).where(attribute_preset_items.c.preset_uuid == preset_uuid))
    await db.delete(preset)
    await db.commit()
    logger.info(f"Preset de atributos {preset_uuid} eliminado via /admin.")


async def list_presets(db: AsyncSession, *, search: str | None, limit: int, offset: int) -> tuple[int, list[AttributePreset]]:
    stmt = select(AttributePreset)
    if search:
        stmt = stmt.where(AttributePreset.name.ilike(f"%{_escape_ilike(search)}%", escape="\\"))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.order_by(func.lower(AttributePreset.name)).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def replace_preset_items(db: AsyncSession, preset_uuid: str, items: list[dict]) -> None:
    """Reemplaza el conjunto COMPLETO de atributos del preset (no incremental) - mismo
    patron que taxonomy_service.replace_category_products."""
    preset = await db.get(AttributePreset, preset_uuid)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preset no encontrado.")

    attribute_uuids = sorted({item["attribute_uuid"] for item in items})
    if attribute_uuids:
        result = await db.execute(select(Attribute.uuid).where(Attribute.uuid.in_(attribute_uuids)))
        found = {row[0] for row in result.all()}
        missing = [u for u in attribute_uuids if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Atributos no encontrados: {', '.join(missing)}")

    await db.execute(delete(attribute_preset_items).where(attribute_preset_items.c.preset_uuid == preset_uuid))
    if items:
        await db.execute(
            attribute_preset_items.insert(),
            [
                {
                    "preset_uuid": preset_uuid,
                    "attribute_uuid": item["attribute_uuid"],
                    "is_required": item.get("is_required", False),
                    "display_order": item.get("display_order", 0),
                }
                for item in items
            ],
        )
    await db.commit()
    logger.info(f"Preset {preset_uuid}: atributos reemplazados via /admin ({len(items)} atributos).")


async def list_preset_items(db: AsyncSession, preset_uuid: str) -> list[dict]:
    preset = await db.get(AttributePreset, preset_uuid)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preset no encontrado.")

    stmt = (
        select(attribute_preset_items.c.attribute_uuid, attribute_preset_items.c.is_required, attribute_preset_items.c.display_order, Attribute)
        .join(Attribute, Attribute.uuid == attribute_preset_items.c.attribute_uuid)
        .where(attribute_preset_items.c.preset_uuid == preset_uuid)
        .order_by(attribute_preset_items.c.display_order)
    )
    result = await db.execute(stmt)
    return [
        {"attribute_uuid": row.attribute_uuid, "is_required": row.is_required, "display_order": row.display_order, "attribute": row.Attribute}
        for row in result.all()
    ]


async def apply_preset_to_products(db: AsyncSession, preset_uuid: str, product_uuids: list[str]) -> tuple[list[str], int]:
    """Agrega las claves de atributos del preset a cada producto con valor null, SOLO para
    las claves que el producto todavia no tiene - un valor ya guardado nunca se
    sobreescribe. `scaffolded_count` cuenta pares (producto, atributo) realmente
    agregados."""
    preset = await db.get(AttributePreset, preset_uuid)
    if preset is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Preset no encontrado.")

    slug_result = await db.execute(
        select(Attribute.slug).select_from(attribute_preset_items).join(Attribute, Attribute.uuid == attribute_preset_items.c.attribute_uuid)
        .where(attribute_preset_items.c.preset_uuid == preset_uuid)
    )
    preset_slugs = {row[0] for row in slug_result.all()}
    if not preset_slugs:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Este preset no tiene atributos asignados.")

    unique_uuids = sorted(set(product_uuids))
    result = await db.execute(
        select(Product.id, Product.sicar_uuid, Product.attributes).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False)
    )
    rows = result.all()
    found = {row.sicar_uuid: row for row in rows}
    missing = [u for u in unique_uuids if u not in found]
    if missing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")

    scaffolded_count = 0
    product_ids: list[int] = []
    for row in found.values():
        existing_keys = set((row.attributes or {}).keys())
        scaffolded_count += len(preset_slugs - existing_keys)
        product_ids.append(row.id)

    if product_ids:
        # El operando IZQUIERDO gana en `||` para claves duplicadas en Postgres - por eso las
        # claves nuevas (con valor null) van a la izquierda y attributes existente a la derecha,
        # asi un valor ya guardado nunca se pisa con null.
        new_keys_json = json.dumps({slug: None for slug in preset_slugs})
        await db.execute(
            update(Product)
            .where(Product.id.in_(product_ids))
            .values(attributes=func.cast(new_keys_json, JSONB).op("||")(func.coalesce(Product.attributes, func.cast("{}", JSONB))))
        )
    await db.commit()
    logger.info(f"Preset {preset_uuid} aplicado via /admin a {len(unique_uuids)} producto(s), {scaffolded_count} clave(s) nueva(s).")
    return unique_uuids, scaffolded_count


# --- Product attribute values: Product.attributes (JSONB), clave=Attribute.slug -----

def coerce_and_validate_value(attribute: Attribute, value):
    """Valida `value` (ya deserializado del JSON del request) contra attribute.data_type/
    allowed_values. Devuelve el valor tal cual (se guarda directo en el JSONB) o lanza
    ValueError con un mensaje listo para mostrarse."""
    if value is None:
        return None
    if attribute.data_type == "TEXT":
        if not isinstance(value, str):
            raise ValueError(f"'{attribute.slug}' espera texto (TEXT).")
        return value
    if attribute.data_type == "NUMBER":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"'{attribute.slug}' espera un numero (NUMBER).")
        return value
    if attribute.data_type == "BOOLEAN":
        if not isinstance(value, bool):
            raise ValueError(f"'{attribute.slug}' espera verdadero/falso (BOOLEAN).")
        return value
    if attribute.data_type == "ENUM":
        if not isinstance(value, str) or value not in (attribute.allowed_values or []):
            options = ", ".join(attribute.allowed_values or [])
            raise ValueError(f"'{attribute.slug}' espera uno de: {options}.")
        return value
    raise ValueError(f"'{attribute.slug}' tiene un dataType desconocido.")


async def get_attributes_for_product(db: AsyncSession, product: Product) -> list[dict]:
    """Igual que `get_product_attributes` pero recibe el `Product` ya cargado - usado por
    `GET /products/{uuid}` (publico), que ya hizo su propio lookup, para evitar una
    segunda consulta redundante. Las claves siempre resuelven contra `attributes` porque
    delete_attribute bloquea el borrado mientras siga en uso - se ignoran en silencio solo
    como salvaguarda defensiva, no un caso esperado."""
    stored = product.attributes or {}
    if not stored:
        return []

    result = await db.execute(select(Attribute).where(Attribute.slug.in_(stored.keys())))
    by_slug = {a.slug: a for a in result.scalars().all()}

    docs = [
        {"attribute_uuid": attr.uuid, "name": attr.name, "slug": attr.slug, "data_type": attr.data_type, "unit": attr.unit, "value": stored[slug]}
        for slug, attr in by_slug.items()
    ]
    docs.sort(key=lambda d: d["name"].lower())
    return docs


async def get_product_attributes(db: AsyncSession, product_uuid: str) -> list[dict]:
    """Vista de solo lectura para el admin: resuelve el producto por sicar_uuid y delega en
    `get_attributes_for_product`."""
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")

    return await get_attributes_for_product(db, product)


async def replace_product_attributes(db: AsyncSession, product_uuid: str, values: list[dict]) -> list[dict]:
    """Reemplaza el conjunto COMPLETO de Product.attributes (no incremental). Cada
    `attributeUuid` debe resolver contra el catalogo (404 nombrando los que falten); cada
    valor se valida contra el dataType/allowedValues del atributo referenciado (422
    nombrando los que no cuadren) antes de escribir nada."""
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")

    attribute_uuids = sorted({v["attribute_uuid"] for v in values})
    attributes_by_uuid: dict[str, Attribute] = {}
    if attribute_uuids:
        result = await db.execute(select(Attribute).where(Attribute.uuid.in_(attribute_uuids)))
        attributes_by_uuid = {a.uuid: a for a in result.scalars().all()}
        missing = [u for u in attribute_uuids if u not in attributes_by_uuid]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Atributos no encontrados: {', '.join(missing)}")

    new_attributes: dict = {}
    errors: list[str] = []
    for v in values:
        attribute = attributes_by_uuid[v["attribute_uuid"]]
        try:
            new_attributes[attribute.slug] = coerce_and_validate_value(attribute, v["value"])
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="; ".join(errors))

    product.attributes = new_attributes or None
    await db.commit()
    await db.refresh(product)
    logger.info(f"Producto {product_uuid}: atributos reemplazados via /admin ({len(new_attributes)} atributos).")
    return await get_product_attributes(db, product_uuid)


# --- Variant groups: vinculo explicito entre SKUs de Sicar X que son la misma pieza en variantes -----

async def create_variant_group(db: AsyncSession, name: str, variant_attribute_slug: str | None) -> VariantGroup:
    group = VariantGroup(
        uuid=str(uuid_lib.uuid4()),
        name=name,
        variant_attribute_slug=variant_attribute_slug,
        updated_at=datetime.now(timezone.utc),
    )
    db.add(group)
    await db.commit()
    await db.refresh(group)
    logger.info(f"Grupo de variantes '{group.name}' ({group.uuid}) creado via /admin.")
    return group


async def update_variant_group(db: AsyncSession, variant_group_uuid: str, data: dict) -> VariantGroup:
    group = await db.get(VariantGroup, variant_group_uuid)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    if "name" in data:
        if not data["name"]:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="name no puede estar vacio.")
        group.name = data["name"]
    if "variant_attribute_slug" in data:
        group.variant_attribute_slug = data["variant_attribute_slug"]

    group.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(group)
    logger.info(f"Grupo de variantes {group.uuid} actualizado via /admin.")
    return group


async def delete_variant_group(db: AsyncSession, variant_group_uuid: str) -> None:
    group = await db.get(VariantGroup, variant_group_uuid)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    # Solo cuenta productos ACTIVOS como "todavia asignado" - filtra is_deleted igual que
    # list_variant_group_products/get_variant_group_detail, para que un producto
    # discontinuado por el sync (ya invisible en GET .../products) no bloquee para siempre
    # el borrado de un grupo que el admin ve vacio.
    has_active_products = await db.scalar(
        select(Product.id).where(Product.variant_group_uuid == variant_group_uuid, Product.is_deleted == False).limit(1)
    )
    if has_active_products:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Este grupo tiene productos asignados; quitalos primero.")

    # Lo unico que puede seguir apuntando aqui a esta altura es un producto soft-deleted -
    # se desvincula antes de borrar, si no la FK de products.variant_group_uuid rechazaria
    # el DELETE con un IntegrityError real en vez del 409 controlado de arriba.
    await db.execute(update(Product).where(Product.variant_group_uuid == variant_group_uuid).values(variant_group_uuid=None))

    await db.delete(group)
    await db.commit()
    logger.info(f"Grupo de variantes {variant_group_uuid} eliminado via /admin.")


async def list_variant_groups(db: AsyncSession, *, search: str | None, limit: int, offset: int) -> tuple[int, list[VariantGroup]]:
    stmt = select(VariantGroup)
    if search:
        stmt = stmt.where(VariantGroup.name.ilike(f"%{_escape_ilike(search)}%", escape="\\"))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    result = await db.execute(stmt.order_by(func.lower(VariantGroup.name)).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def replace_variant_group_products(db: AsyncSession, variant_group_uuid: str, product_uuids: list[str]) -> list[str]:
    """Reemplaza el conjunto COMPLETO de miembros del grupo: limpia variant_group_uuid de
    quien ya no este en la lista y lo asigna a quien si - a diferencia de
    product_categories/product_vehicles (N:M via tabla pivote), aqui es una FK directa en
    Product, asi que "reemplazar" significa reasignar la columna, no borrar+insertar filas."""
    group = await db.get(VariantGroup, variant_group_uuid)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    unique_uuids = sorted(set(product_uuids))
    product_ids: list[int] = []
    if unique_uuids:
        result = await db.execute(select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_uuids), Product.is_deleted == False))
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_uuids if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        product_ids = [found[u] for u in unique_uuids]

    await db.execute(update(Product).where(Product.variant_group_uuid == variant_group_uuid).values(variant_group_uuid=None))
    if product_ids:
        await db.execute(update(Product).where(Product.id.in_(product_ids)).values(variant_group_uuid=variant_group_uuid))
    await db.commit()
    logger.info(f"Grupo de variantes {variant_group_uuid}: productos reemplazados via /admin ({len(product_ids)} productos).")
    return unique_uuids


async def patch_variant_group_products(db: AsyncSession, variant_group_uuid: str, add_uuids: list[str], remove_uuids: list[str]) -> tuple[list[str], list[str], int, int]:
    """Incremental (a diferencia de replace_variant_group_products): agrega/quita sin
    tocar el resto de miembros del grupo - pensado para grupos con mas productos
    asignados que el limite de GET .../products. `add` reasigna incondicionalmente
    (mismo criterio que el PUT: si un producto ya estaba en otro grupo, esta llamada
    gana). `remove` lleva guarda `variant_group_uuid == este grupo` - a diferencia del
    PUT, que limpia el grupo entero antes de reasignar, aqui solo opera sobre el
    subconjunto pedido, asi que sin esta guarda podria quitarle por accidente la
    pertenencia a un producto que mientras tanto ya fue movido a OTRO grupo."""
    group = await db.get(VariantGroup, variant_group_uuid)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    unique_add = sorted(set(add_uuids))
    add_product_ids: list[int] = []
    if unique_add:
        result = await db.execute(select(Product.id, Product.sicar_uuid).where(Product.sicar_uuid.in_(unique_add), Product.is_deleted == False))
        found = {row.sicar_uuid: row.id for row in result.all()}
        missing = [u for u in unique_add if u not in found]
        if missing:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Productos no encontrados: {', '.join(missing)}")
        add_product_ids = [found[u] for u in unique_add]

    added_count = 0
    if add_product_ids:
        result = await db.execute(
            update(Product).where(Product.id.in_(add_product_ids)).values(variant_group_uuid=variant_group_uuid).returning(Product.id)
        )
        added_count = len(result.all())

    unique_remove = sorted(set(remove_uuids))
    removed_count = 0
    if unique_remove:
        result = await db.execute(
            update(Product)
            .where(
                Product.variant_group_uuid == variant_group_uuid,
                Product.sicar_uuid.in_(unique_remove),
            )
            .values(variant_group_uuid=None)
            .returning(Product.id)
        )
        removed_count = len(result.all())

    await db.commit()
    logger.info(
        f"Grupo de variantes {variant_group_uuid}: {added_count} producto(s) agregado(s), "
        f"{removed_count} quitado(s) via PATCH /admin."
    )
    return unique_add, unique_remove, added_count, removed_count


async def list_variant_group_products(db: AsyncSession, variant_group_uuid: str, limit: int, offset: int) -> tuple[int, list[Product]]:
    group = await db.get(VariantGroup, variant_group_uuid)
    if group is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    base = select(Product).where(Product.variant_group_uuid == variant_group_uuid, Product.is_deleted == False)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    result = await db.execute(base.order_by(Product.name).limit(limit).offset(offset))
    return total or 0, list(result.scalars().all())


async def set_product_variant_group(db: AsyncSession, product_uuid: str, variant_group_uuid: str | None) -> Product:
    product = await db.scalar(select(Product).where(Product.sicar_uuid == product_uuid, Product.is_deleted == False))
    if product is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Producto no encontrado.")

    if variant_group_uuid is not None:
        group = await db.get(VariantGroup, variant_group_uuid)
        if group is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grupo de variantes no encontrado.")

    product.variant_group_uuid = variant_group_uuid
    await db.commit()
    await db.refresh(product)
    logger.info(f"Producto {product_uuid}: variant_group_uuid={variant_group_uuid} via /admin.")
    return product


# --- Publico (GET /products/{uuid}): siblings del producto dentro de su grupo de variantes -----

async def get_variant_group_detail(db: AsyncSession, product: Product) -> dict | None:
    """None si el producto no pertenece a ningun grupo. `siblings` excluye al producto
    mismo, solo incluye productos activos/no eliminados."""
    if product.variant_group_uuid is None:
        return None

    group = await db.get(VariantGroup, product.variant_group_uuid)
    if group is None:
        return None

    result = await db.execute(
        select(Product).where(
            Product.variant_group_uuid == group.uuid,
            Product.id != product.id,
            Product.is_deleted == False,
            Product.is_active == True,
        ).order_by(Product.name)
    )
    siblings = list(result.scalars().all())

    def _value_for(p: Product):
        if not group.variant_attribute_slug:
            return None
        return (p.attributes or {}).get(group.variant_attribute_slug)

    return {
        "uuid": group.uuid,
        "name": group.name,
        "variant_attribute_slug": group.variant_attribute_slug,
        "siblings": [
            {
                "uuid": s.sicar_uuid,
                "sku": s.sku,
                "name": s.name,
                "image_url": s.image_url,
                "price": s.price,
                "stock": s.available_stock,
                "value": _value_for(s),
            }
            for s in siblings
        ],
    }

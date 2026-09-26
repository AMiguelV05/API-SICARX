"""Triggers de la migracion c4e8a2f6b1d9 que marcan products.search_dirty_at (pendiente de
reindexar en Typesense) - ver CLAUDE.md, "Busqueda con Typesense".

Todo el test corre en una sola transaccion (fixture `db`), asi que now() es constante
dentro de el: cada paso limpia la bandera y afirma NULL / NOT NULL, nunca timestamps."""
from decimal import Decimal

from sqlalchemy import text, update

from app.models.product import Product
from app.models.taxonomy import Category, product_categories
from app.models.vehicle import Vehicle, product_vehicles
from tests.conftest import make_product


async def _dirty(db, product) -> bool:
    return await db.scalar(
        text("SELECT search_dirty_at IS NOT NULL FROM products WHERE id = :id"), {"id": product.id}
    )


async def _clear(db, product) -> None:
    await db.execute(text("UPDATE products SET search_dirty_at = NULL WHERE id = :id"), {"id": product.id})


async def _new_clean_product(db, **overrides) -> Product:
    product = make_product(**overrides)
    db.add(product)
    await db.flush()
    await _clear(db, product)
    assert not await _dirty(db, product)
    return product


async def test_insert_marks_dirty(db):
    product = make_product()
    db.add(product)
    await db.flush()
    assert await _dirty(db, product)


async def test_name_change_marks_dirty(db):
    product = await _new_clean_product(db)
    await db.execute(update(Product).where(Product.id == product.id).values(name="Martillo nuevo"))
    assert await _dirty(db, product)


async def test_each_search_column_marks_dirty(db):
    product = await _new_clean_product(db, brand="Truper", description="desc", additional_skus=["A1"])
    changes = [
        {"sku": "NUEVO-SKU"},
        {"additional_skus": ["A1", "A2"]},
        {"brand": "Pretul"},
        {"brand": None},
        {"description": "otra"},
        {"price": Decimal("123.45")},
        {"is_active": False},
        {"is_deleted": True},
        {"department_uuid": "dep-x"},
        {"category_uuid": "cat-x"},
        {"sales_count": Decimal("5")},
    ]
    for values in changes:
        await db.execute(update(Product).where(Product.id == product.id).values(**values))
        assert await _dirty(db, product), f"no marco dirty al cambiar {values}"
        await _clear(db, product)


async def test_sync_style_update_of_unindexed_columns_does_not_mark_dirty(db):
    """El upsert de sync_task.py reescribe las ~124k filas cada 5 minutos con un
    last_sync_id nuevo; si eso marcara todo, el worker reindexaria el catalogo entero cada
    vez. Solo last_sync_id (y valores identicos) cambian aqui."""
    product = await _new_clean_product(db, name="Llave española", price=Decimal("50.00"))
    await db.execute(
        update(Product).where(Product.id == product.id).values(
            last_sync_id="pase-nuevo", name="Llave española", price=Decimal("50.00"),
            image_url="https://img.example/x.jpg", details_updated_at=None,
        )
    )
    assert not await _dirty(db, product)


async def test_reserved_change_that_keeps_stock_available_does_not_mark_dirty(db):
    product = await _new_clean_product(db, stock=Decimal("10"), reserved=Decimal("0"))
    await db.execute(update(Product).where(Product.id == product.id).values(reserved=Decimal("3")))
    assert not await _dirty(db, product)


async def test_reserved_change_that_crosses_zero_marks_dirty(db):
    product = await _new_clean_product(db, stock=Decimal("10"), reserved=Decimal("0"))
    await db.execute(update(Product).where(Product.id == product.id).values(reserved=Decimal("10")))
    assert await _dirty(db, product)

    # Y de vuelta: agotado -> disponible tambien cuenta.
    await _clear(db, product)
    await db.execute(update(Product).where(Product.id == product.id).values(reserved=Decimal("4")))
    assert await _dirty(db, product)


async def test_restock_from_zero_marks_dirty(db):
    product = await _new_clean_product(db, stock=Decimal("0"), reserved=Decimal("0"))
    await db.execute(update(Product).where(Product.id == product.id).values(stock=Decimal("7")))
    assert await _dirty(db, product)


async def test_clearing_the_flag_does_not_re_mark_it(db):
    product = make_product()
    db.add(product)
    await db.flush()
    await _clear(db, product)
    assert not await _dirty(db, product)


async def test_product_categories_insert_and_delete_mark_dirty(db):
    from datetime import datetime, timezone

    category = Category(
        uuid="cat-trigger-test", name="Categoria prueba", slug="categoria-prueba-trigger",
        updated_at=datetime.now(timezone.utc),
    )
    db.add(category)
    p1 = await _new_clean_product(db)
    p2 = await _new_clean_product(db)
    untouched = await _new_clean_product(db)

    await db.execute(product_categories.insert().values(
        [{"category_uuid": category.uuid, "product_id": p1.id},
         {"category_uuid": category.uuid, "product_id": p2.id}]
    ))
    assert await _dirty(db, p1)
    assert await _dirty(db, p2)
    assert not await _dirty(db, untouched)

    await _clear(db, p1)
    await _clear(db, p2)
    await db.execute(product_categories.delete().where(product_categories.c.product_id == p1.id))
    assert await _dirty(db, p1)
    assert not await _dirty(db, p2)


async def test_product_vehicles_insert_and_delete_mark_dirty(db):
    from datetime import datetime, timezone

    vehicle = Vehicle(
        uuid="veh-trigger-test", vehicle_type="AUTOMOTIVE", make="Chevrolet", model="Aveo",
        year_start=2008, year_end=2016, engine="L4 1.6L", updated_at=datetime.now(timezone.utc),
    )
    db.add(vehicle)
    product = await _new_clean_product(db)

    await db.execute(product_vehicles.insert().values(vehicle_uuid=vehicle.uuid, product_id=product.id))
    assert await _dirty(db, product)

    await _clear(db, product)
    await db.execute(product_vehicles.delete().where(product_vehicles.c.product_id == product.id))
    assert await _dirty(db, product)


async def test_seed_synonyms_exist(db):
    rows = (await db.execute(text("SELECT root, synonyms FROM search_synonyms ORDER BY id"))).all()
    as_sets = {(root, frozenset(words)) for root, words in rows}
    assert (None, frozenset({"cuadro", "entrada", "mando"})) in as_sets
    assert (None, frozenset({"allen", "hexagonal"})) in as_sets
    assert ("inalambrico", frozenset({"bateria", "20v", "12v"})) in as_sets
    assert (None, frozenset({"desarmador", "destornillador"})) in as_sets

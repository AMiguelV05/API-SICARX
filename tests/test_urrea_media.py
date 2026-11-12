"""Fase 2 de URREA: foto de respaldo (Product.effective_image_url) y medios del proveedor en
el detalle de producto."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import select, text

from app.models.product import Product
from app.models.supplier_product import SupplierProduct
from tests.conftest import make_product


def _fresh(**overrides) -> Product:
    # details_updated_at reciente: GET /products/{uuid} no intenta el refresh contra Sicar X.
    return make_product(details_updated_at=datetime.now(timezone.utc), **overrides)


async def test_effective_image_url_prefers_sicar_and_falls_back_to_supplier(db):
    brand = f"MarcaFoto-{uuid.uuid4().hex[:6]}"
    sicar = _fresh(brand=brand, name="A con foto Sicar", image_url="https://sicar.test/a.jpg", fallback_image_url="https://urrea.test/a.jpg")
    fallback = _fresh(brand=brand, name="B con foto URREA", image_url="", fallback_image_url="https://urrea.test/b.jpg")
    none = _fresh(brand=brand, name="C sin foto", image_url=None)
    db.add_all([sicar, fallback, none])
    await db.flush()

    assert sicar.effective_image_url == "https://sicar.test/a.jpg"
    assert fallback.effective_image_url == "https://urrea.test/b.jpg"
    assert none.effective_image_url is None
    rows = dict((await db.execute(
        select(Product.name, Product.effective_image_url).where(Product.brand == brand)
    )).all())
    assert rows == {"A con foto Sicar": "https://sicar.test/a.jpg", "B con foto URREA": "https://urrea.test/b.jpg", "C sin foto": None}


async def test_catalog_and_available_now_use_the_fallback_photo(db, api):
    brand = f"MarcaFoto-{uuid.uuid4().hex[:6]}"
    department = str(uuid.uuid4())
    # Mismo sales_count: "con foto primero" decide el orden de relevance.
    common = dict(brand=brand, department_uuid=department, image_url=None, sales_count=5)
    without = _fresh(name="Sin foto", **common)
    fallback = _fresh(name="Foto URREA", fallback_image_url="https://urrea.test/x.jpg", **common)
    db.add_all([without, fallback])
    await db.flush()

    docs = (await api.post("/v1/products", json={"brand": brand, "sortBy": "relevance"})).json()["docs"]
    assert [d["name"] for d in docs] == ["Foto URREA", "Sin foto"]
    assert docs[0]["imageUrl"] == "https://urrea.test/x.jpg"
    assert docs[1]["imageUrl"] is None

    available = (await api.get("/v1/products/available-now", params={"departmentUuid": department})).json()["docs"]
    assert [d["sicarUuid"] for d in available] == [fallback.sicar_uuid]


async def test_detail_exposes_fallback_photo_and_supplier_media(db, api):
    product = _fresh(image_url=None, fallback_image_url="https://urrea.test/d.jpg")
    plain = _fresh()
    db.add_all([product, plain])
    await db.flush()
    db.add(SupplierProduct(
        supplier="URREA", supplier_code=f"T{product.id}", product_id=product.id, match_method="SKU_PREFIX",
        list_price=57, video_url="https://www.youtube.com/embed/aSYB_sNFd_U", datasheet_url="https://urrea.test/ficha.pdf",
    ))
    await db.flush()

    body = (await api.get(f"/v1/products/{product.sicar_uuid}")).json()
    assert body["imageUrl"] == "https://urrea.test/d.jpg"
    assert body["supplierMedia"] == {
        "videoUrl": "https://www.youtube.com/embed/aSYB_sNFd_U", "datasheetUrl": "https://urrea.test/ficha.pdf", "manualUrl": None,
    }
    assert "listPrice" not in str(body) and "57" not in str(body.get("supplierMedia"))

    assert (await api.get(f"/v1/products/{plain.sicar_uuid}")).json()["supplierMedia"] is None


async def test_search_trigger_marks_dirty_only_when_effective_photo_appears(db):
    with_sicar = _fresh(image_url="https://sicar.test/a.jpg")
    without = _fresh(image_url=None)
    db.add_all([with_sicar, without])
    await db.flush()
    await db.execute(text("UPDATE products SET search_dirty_at = NULL WHERE id IN (:a, :b)"), {"a": with_sicar.id, "b": without.id})

    await db.execute(
        text("UPDATE products SET fallback_image_url = 'https://urrea.test/f.jpg' WHERE id IN (:a, :b)"),
        {"a": with_sicar.id, "b": without.id},
    )
    dirty = dict((await db.execute(
        text("SELECT id, search_dirty_at IS NOT NULL FROM products WHERE id IN (:a, :b)"), {"a": with_sicar.id, "b": without.id}
    )).all())
    # Ya tenia foto de Sicar X: no cambia has_image, no se reindexa.
    assert dirty == {with_sicar.id: False, without.id: True}


async def test_search_document_has_image_counts_the_fallback_photo(db):
    from app.services.search_index import fetch_documents_by_ids

    fallback = _fresh(image_url="", fallback_image_url="https://urrea.test/s.jpg")
    none = _fresh(image_url=None)
    db.add_all([fallback, none])
    await db.flush()

    docs = {d["id"]: d for d in await fetch_documents_by_ids(db, [fallback.id, none.id])}
    assert docs[fallback.sicar_uuid]["has_image"] is True
    assert docs[none.sicar_uuid]["has_image"] is False

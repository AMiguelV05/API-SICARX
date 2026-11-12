"""Sync de la masiva de URREA (urrea_sync_worker.sync_bulk_catalog) y su aplicacion al PIM
(urrea_pim_service), contra Postgres real y una masiva escrita a disco."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.product import Product
from app.models.supplier_product import SupplierProduct, SupplierSyncStatus
from app.services.urrea_pim_service import format_bullets
from app.worker import urrea_sync_worker
from app.worker.urrea_sync_worker import is_due, sync_bulk_catalog
from tests.conftest import make_product


def _row(code: str, **overrides) -> dict:
    row = {
        "codigo": code, "nombreLargo": f"Nombre largo {code}", "DescripcionProducto": f"Descripcion URREA {code}",
        "Marca": "Urrea", "Submarca": "Urrea", "familia": "Herramientas", "clase": "Llaves", "Subclase": "Combinadas",
        "Precio": "57.00", "Moneda": "MXP", "Multiplo": "6", "CodigoBarras": f"750{code}", "CodigoBarrasSinCero": f"750{code}",
        "EstatusInventario": "Disponible", "anexo20SAT": "27111713", "claveUnidadSAT": "EA",
        "bullets": "-Pulido espejo -Cromo-Vanadio", "esJuego": "No", "piezasJuego": "0", "contenidoJuego": "",
        "accesorios": "Llave combinada.", "garantia": "1 año", "empaque": "Granel", "keywords": "llave",
        "fotografia": [f"https://blob.test/{code}.jpg"], "video": "https://www.youtube.com/embed/abc",
        "fichaTecnica": "https://blob.test/ficha.pdf", "manual": "", "alto": "1", "fondo": "2.7", "ancho": "18", "peso": "0.1",
        "caracteristica": "",
    }
    row.update(overrides)
    return row


def _write(tmp_path, rows: list[dict], name: str = "masiva.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps({"status": "OK", "resultadoDispMasiva": rows}), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _prefix(monkeypatch):
    monkeypatch.setattr(settings, "URREA_SKU_PREFIX", "U")


async def _supplier_row(db, code: str) -> SupplierProduct | None:
    return await db.scalar(
        select(SupplierProduct).where(SupplierProduct.supplier_code == code).execution_options(populate_existing=True)
    )


async def _product(db, product_id: int) -> Product:
    return await db.scalar(select(Product).where(Product.id == product_id).execution_options(populate_existing=True))


def test_format_bullets_splits_only_on_leading_dashes():
    assert format_bullets("-Pulido espejo -Cromo-Vanadio. -Medida 1-9/16 in") == "-Pulido espejo\n-Cromo-Vanadio.\n-Medida 1-9/16 in"
    assert format_bullets("Sin guiones") == "-Sin guiones"
    assert format_bullets("  ") is None


async def test_first_application_replaces_pim_and_sets_supplier_state(db, tmp_path):
    product = make_product(
        sku="UT100", description=None, brand="Surtek", bullet_points="-Viejo", contents="viejo",
        technical_specs="-Centro: 7/8", package_weight_kg=Decimal("0.686"), package_length_cm=Decimal("5"),
        package_width_cm=Decimal("5"), package_height_cm=Decimal("6.5"),
    )
    db.add(product)
    await db.flush()

    result = await sync_bulk_catalog(db, _write(tmp_path, [_row("T100"), _row("NOEXISTE")]), commit=False)

    assert (result.received, result.matched, result.changed) == (2, 1, 1)
    p = await _product(db, product.id)
    assert p.description == "Descripcion URREA T100"
    assert p.brand == "Urrea"
    assert p.bullet_points == "-Pulido espejo\n-Cromo-Vanadio"
    assert p.contents == "Llave combinada."
    assert p.technical_specs == "-Centro: 7/8"  # nunca se toca
    # Las medidas solo se llenan si estan vacias, tambien la primera vez (la plantilla es mas precisa).
    assert (p.package_weight_kg, p.package_length_cm, p.package_width_cm, p.package_height_cm) == (
        Decimal("0.686"), Decimal("5.00"), Decimal("5.00"), Decimal("6.50"))
    assert p.supplier_available is True
    assert p.fallback_image_url == "https://blob.test/T100.jpg"

    s = await _supplier_row(db, "T100")
    assert s.product_id == product.id
    assert s.match_method == "SKU_PREFIX"
    assert s.list_price == Decimal("57.0000")
    assert s.sale_multiple == 6
    assert s.availability_status == "AVAILABLE"
    assert s.video_url == "https://www.youtube.com/embed/abc"
    assert s.datasheet_url == "https://blob.test/ficha.pdf"
    assert sorted(s.pim_fields_applied) == ["brand", "bullet_points", "contents", "description", "package_dimensions"]


async def test_first_application_keeps_local_value_when_urrea_is_empty(db, tmp_path):
    product = make_product(sku="UT101", contents="contenido local", bullet_points="-Local")
    db.add(product)
    await db.flush()

    await sync_bulk_catalog(db, _write(tmp_path, [_row("T101", accesorios="", bullets="", peso="0")]), commit=False)

    p = await _product(db, product.id)
    assert p.contents == "contenido local"
    assert p.bullet_points == "-Local"
    assert p.package_weight_kg is None  # medidas invalidas de URREA: no se aplican
    s = await _supplier_row(db, "T101")
    assert "contents" not in s.pim_fields_applied
    assert "package_dimensions" not in s.pim_fields_applied


async def test_later_runs_only_fill_empty_fields_never_applied(db, tmp_path):
    product = make_product(sku="UT102")
    db.add(product)
    await db.flush()
    await sync_bulk_catalog(db, _write(tmp_path, [_row("T102", accesorios="")]), commit=False)

    # Sin medidas locales: la primera aplicacion las llena (fondo -> largo).
    p = await _product(db, product.id)
    assert (p.package_weight_kg, p.package_length_cm, p.package_width_cm, p.package_height_cm) == (
        Decimal("0.100"), Decimal("2.70"), Decimal("18.00"), Decimal("1.00"))

    # El admin edita la descripcion y borra la marca.
    p.description = "Editada por admin"
    p.brand = None
    await db.flush()

    second = _row("T102", DescripcionProducto="Nueva descripcion URREA", Marca="Otra", accesorios="Ahora con accesorios")
    await sync_bulk_catalog(db, _write(tmp_path, [second], "segunda.json"), commit=False)

    p = await _product(db, product.id)
    assert p.description == "Editada por admin"
    assert p.brand is None  # ya se habia aplicado: no se vuelve a llenar
    assert p.contents == "Ahora con accesorios"  # nunca aplicado y vacio: se llena
    s = await _supplier_row(db, "T102")
    assert "contents" in s.pim_fields_applied
    assert s.info["description"] == "Nueva descripcion URREA"


async def test_unchanged_rerun_writes_nothing(db, tmp_path):
    db.add(make_product(sku="UT103"))
    await db.flush()
    path = _write(tmp_path, [_row("T103")])
    await sync_bulk_catalog(db, path, commit=False)
    again = await sync_bulk_catalog(db, path, commit=False)
    assert (again.matched, again.changed, again.pim_fields_written) == (1, 0, 0)


async def test_status_change_and_missing_codes(db, tmp_path):
    a = make_product(sku="UT104")
    b = make_product(sku="UT105")
    db.add_all([a, b])
    await db.flush()
    await sync_bulk_catalog(db, _write(tmp_path, [_row("T104"), _row("T105")]), commit=False)

    # T104 pasa a "No disponible" y T105 desaparece de la masiva.
    await sync_bulk_catalog(db, _write(tmp_path, [_row("T104", EstatusInventario="No disponible")], "b.json"), commit=False)

    assert (await _product(db, a.id)).supplier_available is False
    assert (await _supplier_row(db, "T104")).availability_status == "UNAVAILABLE"
    assert (await _product(db, b.id)).supplier_available is False
    gone = await _supplier_row(db, "T105")
    assert gone.availability_status is None
    assert gone.pim_fields_applied is not None  # la fila se conserva


async def test_empty_bulk_clears_nothing(db, tmp_path):
    product = make_product(sku="UT106")
    db.add(product)
    await db.flush()
    await sync_bulk_catalog(db, _write(tmp_path, [_row("T106")]), commit=False)
    await sync_bulk_catalog(db, _write(tmp_path, [], "vacia.json"), commit=False)
    assert (await _product(db, product.id)).supplier_available is True


async def test_barcode_fallback_and_duplicate_codes(db, tmp_path):
    by_barcode = make_product(sku="OTRO-SKU", additional_skus=["7509999"])
    db.add(by_barcode)
    await db.flush()
    rows = [_row("ZZ1", CodigoBarras="7509999"), _row("ZZ2", CodigoBarras="7509999")]
    result = await sync_bulk_catalog(db, _write(tmp_path, rows), commit=False)
    assert (result.matched, result.duplicates) == (1, 1)
    s = await _supplier_row(db, "ZZ1")
    assert (s.product_id, s.match_method) == (by_barcode.id, "BARCODE")
    assert await _supplier_row(db, "ZZ2") is None


def test_is_due():
    now = datetime.now(timezone.utc)
    assert is_due(None, now)
    recent = SupplierSyncStatus(last_run_started_at=now - timedelta(minutes=10))
    assert not is_due(recent, now)
    assert is_due(SupplierSyncStatus(last_run_started_at=now - timedelta(hours=3)), now)
    failed = SupplierSyncStatus(last_run_started_at=now - timedelta(minutes=31), last_error="boom")
    assert is_due(failed, now)
    requested = SupplierSyncStatus(last_run_started_at=now - timedelta(minutes=5), requested_at=now - timedelta(minutes=1))
    assert is_due(requested, now)


async def test_run_if_due_is_noop_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "URREA_USER", None)

    async def boom():
        raise AssertionError("no debe descargar")

    monkeypatch.setattr(urrea_sync_worker.urrea_client, "download_bulk_catalog", boom)
    await urrea_sync_worker.run_if_due()


# --- Admin --------------------------------------------------------------------------------

async def test_admin_status_sync_request_and_product_supplier(db, api, tmp_path, monkeypatch):
    from tests.conftest import admin_headers

    monkeypatch.setattr(settings, "URREA_USER", "u")
    monkeypatch.setattr(settings, "URREA_PASSWORD", "p")
    product = make_product(sku="UT200", price=Decimal("116.00"))
    db.add(product)
    await db.flush()
    await sync_bulk_catalog(db, _write(tmp_path, [_row("T200", Precio="50.00")]), commit=False)

    super_headers = await admin_headers(db)
    status = (await api.get("/v1/admin/suppliers/urrea/status", headers=super_headers)).json()
    assert status["enabled"] is True
    assert status["linkedProducts"] >= 1

    staff_headers = await admin_headers(db, role="staff")
    assert (await api.post("/v1/admin/suppliers/urrea/sync", headers=staff_headers)).status_code == 403
    response = await api.post("/v1/admin/suppliers/urrea/sync", headers=super_headers)
    assert response.status_code == 202
    status = (await api.get("/v1/admin/suppliers/urrea/status", headers=super_headers)).json()
    assert status["requestedAt"] is not None

    info = (await api.get(f"/v1/admin/products/{product.sicar_uuid}/supplier", headers=staff_headers)).json()
    assert info["supplierCode"] == "T200"
    assert Decimal(str(info["listPrice"])) == Decimal("50")
    assert "marginPercent" not in info and "costPrice" not in info  # el precio de lista no es nuestro costo

    other = make_product(sku="SIN-PROVEEDOR")
    db.add(other)
    await db.flush()
    assert (await api.get(f"/v1/admin/products/{other.sicar_uuid}/supplier", headers=staff_headers)).status_code == 404


async def test_admin_sync_request_conflicts_when_not_configured(db, api, monkeypatch):
    from tests.conftest import admin_headers

    monkeypatch.setattr(settings, "URREA_USER", None)
    response = await api.post("/v1/admin/suppliers/urrea/sync", headers=await admin_headers(db))
    assert response.status_code == 409


async def test_supplier_columns_stay_out_of_the_sicar_sync():
    """supplier_available/fallback_image_url son locales: el upsert de Sicar X no debe tocarlas."""
    import inspect

    from app.worker import sync_task

    source = inspect.getsource(sync_task.sync_sicar_catalog)
    assert "supplier_available" not in source
    assert "fallback_image_url" not in source

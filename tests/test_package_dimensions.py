"""Peso y medidas de empaque (Product.package_*): validacion, constraints, rutas admin,
detalle publico, hoja "Medidas" de la importacion masiva y aislamiento del indice de busqueda."""
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.models.product import Product
from app.services import bulk_import_service
from app.services.package_dimensions_service import validate_dimensions
from tests.conftest import admin_headers, make_product

DIMS = {"weightKg": 5.75, "lengthCm": 22, "widthCm": 19, "heightCm": 36}


def test_validate_dimensions_parses_and_rounds():
    d = validate_dimensions(0.6000000000000001, "6.5", "16 cm", "4,29")
    assert (d.weight_kg, d.length_cm, d.width_cm, d.height_cm) == (Decimal("0.600"), Decimal("6.50"), Decimal("16.00"), Decimal("4.29"))
    assert validate_dimensions("0.3 kg", 1, 1, 1).weight_kg == Decimal("0.300")
    # Positivo que redondearia a 0 (broca de 0.1 g) sube al minimo en vez de rechazarse.
    tiny = validate_dimensions(0.0001, 0.001, 1, 1)
    assert (tiny.weight_kg, tiny.length_cm) == (Decimal("0.001"), Decimal("0.01"))


@pytest.mark.parametrize("weight,length,reason", [
    ("Pendiente", 10, "weightKg: no es un numero"),
    (0, 10, "weightKg debe ser mayor que 0"),
    (-1, 10, "weightKg debe ser mayor que 0"),
    (1, 4500, "lengthCm excede el maximo"),
    (1, "31-33.7", "lengthCm: no es un numero"),
    (1, "1,100", "lengthCm: no es un numero"),  # coma de miles ambigua
    (float("nan"), 10, "weightKg: no es un numero"),
    (None, 10, "weightKg: valor vacio"),
    (1e30, 10, "weightKg excede el maximo"),
])
def test_validate_dimensions_rejects(weight, length, reason):
    with pytest.raises(ValueError, match=reason):
        validate_dimensions(weight, length, 10, 10)


async def test_db_rejects_partial_dimensions(db):
    p = make_product(package_weight_kg=Decimal("1.000"))
    db.add(p)
    with pytest.raises(IntegrityError, match="ck_products_package_dims_all_or_none"):
        await db.flush()


async def test_db_rejects_non_positive_dimensions(db):
    p = make_product(package_weight_kg=Decimal("1"), package_length_cm=Decimal("0"), package_width_cm=Decimal("1"), package_height_cm=Decimal("1"))
    db.add(p)
    with pytest.raises(IntegrityError, match="ck_products_package_dims_positive"):
        await db.flush()


async def test_admin_put_delete_and_public_detail(api, db):
    p = make_product(details_updated_at=datetime.now(timezone.utc))  # sin refresco de Sicar X
    db.add(p)
    await db.flush()
    headers = await admin_headers(db, role="staff")
    url = f"/v1/admin/products/{p.sicar_uuid}/package-dimensions"

    assert (await api.get(f"/v1/products/{p.sicar_uuid}")).json()["packageDimensions"] is None

    r = await api.put(url, json={**DIMS, "weightKg": 0.6000000000000001}, headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"productUuid": p.sicar_uuid, "packageDimensions": {**DIMS, "weightKg": 0.6}}

    detail = (await api.get(f"/v1/products/{p.sicar_uuid}")).json()
    assert detail["packageDimensions"] == {**DIMS, "weightKg": 0.6}

    assert (await api.delete(url, headers=headers)).status_code == 204
    assert (await api.delete(url, headers=headers)).status_code == 204  # idempotente
    assert (await api.get(f"/v1/products/{p.sicar_uuid}")).json()["packageDimensions"] is None


async def test_admin_put_validation_and_404(api, db):
    p = make_product()
    db.add(p)
    await db.flush()
    headers = await admin_headers(db, role="staff")

    partial = await api.put(f"/v1/admin/products/{p.sicar_uuid}/package-dimensions", json={"weightKg": 1}, headers=headers)
    assert partial.status_code == 422
    too_big = await api.put(f"/v1/admin/products/{p.sicar_uuid}/package-dimensions", json={**DIMS, "lengthCm": 601}, headers=headers)
    assert too_big.status_code == 422
    missing = await api.put("/v1/admin/products/no-existe/package-dimensions", json=DIMS, headers=headers)
    assert missing.status_code == 404
    no_auth = await api.put(f"/v1/admin/products/{p.sicar_uuid}/package-dimensions", json=DIMS)
    assert no_auth.status_code == 401


def _medidas_workbook(rows: list[list]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Medidas"
    ws.append(["sku", "weightKg", "lengthCm", "widthCm", "heightCm"])
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def test_bulk_import_medidas_sheet(db):
    a = make_product(sku="UTEST-A")
    b = make_product(sku="UTEST-B", additional_skus=["7500000000017"])
    db.add_all([a, b])
    await db.flush()

    file_bytes = _medidas_workbook([
        ["utest-a", 5.75, 22, 19, 36],          # sku sin distinguir mayusculas
        ["7500000000017", "0,6", "6.5", 6.5, 24],  # fallback por additional_skus, coma decimal
        ["UTEST-A", "Pendiente", 1, 1, 1],      # invalida: no pisa la fila valida anterior
        ["NO-EXISTE", 1, 1, 1, 1],
        ["UTEST-B", 1, 1, 1, None],             # incompleta
    ])
    outcome = await bulk_import_service.import_bulk_assignments(db, file_bytes)
    sheet = outcome.package_dimensions
    assert sheet.found and sheet.processed_rows == 5 and sheet.assigned_count == 2
    assert sorted((e.row, e.reason_code) for e in sheet.errors) == [(4, "INVALID_DIMENSIONS"), (5, "SKU_NOT_FOUND"), (6, "INVALID_DIMENSIONS")]

    await db.refresh(a)
    await db.refresh(b)
    assert (a.package_weight_kg, a.package_length_cm, a.package_width_cm, a.package_height_cm) == (Decimal("5.750"), Decimal("22.00"), Decimal("19.00"), Decimal("36.00"))
    assert b.package_weight_kg == Decimal("0.600") and b.package_height_cm == Decimal("24.00")

    # Re-subir lo mismo no reescribe filas.
    again = await bulk_import_service.import_bulk_assignments(db, _medidas_workbook([["UTEST-A", 5.75, 22, 19, 36]]))
    assert again.package_dimensions.assigned_count == 0


def test_template_includes_medidas_sheet():
    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(bulk_import_service.build_template_workbook()))
    assert [c.value for c in wb["Medidas"][1]] == ["sku", "weightKg", "lengthCm", "widthCm", "heightCm"]


async def test_dimensions_do_not_mark_search_dirty(db):
    p = make_product()
    db.add(p)
    await db.flush()
    await db.execute(update(Product).where(Product.id == p.id).values(search_dirty_at=None))
    await db.execute(update(Product).where(Product.id == p.id).values(
        package_weight_kg=Decimal("1"), package_length_cm=Decimal("2"), package_width_cm=Decimal("3"), package_height_cm=Decimal("4"),
    ))
    assert await db.scalar(select(Product.search_dirty_at).where(Product.id == p.id)) is None

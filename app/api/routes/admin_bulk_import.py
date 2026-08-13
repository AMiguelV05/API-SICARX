import asyncio
import logging
from fastapi import APIRouter, Depends, File, HTTPException, Response, UploadFile, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.bulk_import import BulkImportProductsResponse, BulkImportSheetResult, BulkImportRowError
from app.services import bulk_import_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/bulk-import", tags=["Admin - Bulk Import"], dependencies=[Depends(validate_admin_key)])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _sheet_result(outcome, sheet_name: str) -> BulkImportSheetResult:
    return BulkImportSheetResult(
        found=outcome.found,
        processed_rows=outcome.processed_rows,
        assigned_count=outcome.assigned_count,
        errors=[
            BulkImportRowError(sheet=sheet_name, row=e.row, reason_code=e.reason_code, reason=e.reason, sku=e.sku)
            for e in outcome.errors
        ],
    )


@router.get(
    "/template",
    summary="Descargar una plantilla .xlsx de ejemplo para POST /admin/bulk-import/products",
)
async def admin_bulk_import_template():
    """Descarga un .xlsx de ejemplo con las hojas/columnas exactas que espera `POST
    /products` (comparten las mismas constantes, no pueden desalinearse) - encabezados en
    negritas, comentarios de celda y filas de ejemplo con datos ficticios."""
    file_bytes = await asyncio.to_thread(bulk_import_service.build_template_workbook)
    return Response(
        content=file_bytes,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": "attachment; filename=plantilla_importacion_masiva.xlsx"},
    )


@router.post(
    "/products",
    response_model=BulkImportProductsResponse,
    summary="Asignacion masiva de categorias/vehiculos/atributos/grupos de variantes a productos desde un .xlsx",
)
async def admin_bulk_import_products(db: DbDep, file: UploadFile = File(...)):
    """Importacion masiva desde un .xlsx (hojas opcionales 'Categorias'/'Vehiculos'/
    'Atributos'/'Variantes'), resolviendo productos por `sku` (+ `additional_skus` como
    fallback). Exito parcial: filas invalidas se omiten y se reportan individualmente,
    solo problemas a nivel de archivo completo rechazan todo. Semantica de re-subida
    distinta por hoja: Categorias/Vehiculos son ADITIVOS (`ON CONFLICT DO NOTHING`,
    no-op seguro); Atributos hace MERGE (un valor corregido SI se aplica); Variantes
    REEMPLAZA (`variantGroupUuid` es un solo valor por producto, no un tag)."""
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Se espera un archivo .xlsx.")

    file_bytes = await file.read()
    outcome = await bulk_import_service.import_bulk_assignments(db, file_bytes)
    return BulkImportProductsResponse(
        categories=_sheet_result(outcome.categories, "Categorias"),
        vehicles=_sheet_result(outcome.vehicles, "Vehiculos"),
        attributes=_sheet_result(outcome.attributes, "Atributos"),
        variants=_sheet_result(outcome.variants, "Variantes"),
    )

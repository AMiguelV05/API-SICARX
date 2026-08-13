import asyncio
import logging
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.bulk_import import BulkImportProductsResponse, BulkImportSheetResult, BulkImportRowError
from app.services import bulk_import_service
from app.services.bulk_import_service import MAX_FILE_SIZE_BYTES

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/bulk-import", tags=["Admin - Bulk Import"], dependencies=[Depends(validate_admin_key)])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_READ_CHUNK_SIZE = 1024 * 1024  # 1 MB


async def _read_upload_bounded(file: UploadFile, max_bytes: int) -> bytes:
    """Lee el archivo en chunks, abortando en cuanto se excede max_bytes, en vez de
    bufferear el body entero en memoria antes de siquiera revisar el tamano -
    MAX_FILE_SIZE_BYTES ya era un limite post-lectura (ver
    bulk_import_service.import_bulk_assignments); esto lo hace tambien un limite durante
    la lectura, asi que un archivo gigante nunca llega a estar completo en memoria."""
    chunks = []
    total = 0
    while True:
        chunk = await file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Archivo demasiado grande (limite {max_bytes // (1024 * 1024)} MB).",
            )
        chunks.append(chunk)
    return b"".join(chunks)


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
async def admin_bulk_import_products(request: Request, db: DbDep, file: UploadFile = File(...)):
    """Importacion masiva desde un .xlsx (hojas opcionales 'Categorias'/'Vehiculos'/
    'Atributos'/'Variantes'), resolviendo productos por `sku` (+ `additional_skus` como
    fallback). Exito parcial: filas invalidas se omiten y se reportan individualmente,
    solo problemas a nivel de archivo completo rechazan todo. Semantica de re-subida
    distinta por hoja: Categorias/Vehiculos son ADITIVOS (`ON CONFLICT DO NOTHING`,
    no-op seguro); Atributos hace MERGE (un valor corregido SI se aplica); Variantes
    REEMPLAZA (`variantGroupUuid` es un solo valor por producto, no un tag)."""
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Se espera un archivo .xlsx.")

    # Rechazo rapido por Content-Length, sin leer nada, cuando el header esta presente y
    # ya declara un tamano excesivo - el chunked read de abajo es la proteccion real
    # (Content-Length puede venir ausente con Transfer-Encoding: chunked, o no confiarse).
    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit() and int(content_length) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Archivo demasiado grande (limite {MAX_FILE_SIZE_BYTES // (1024 * 1024)} MB).",
        )

    file_bytes = await _read_upload_bounded(file, MAX_FILE_SIZE_BYTES)
    outcome = await bulk_import_service.import_bulk_assignments(db, file_bytes)
    return BulkImportProductsResponse(
        categories=_sheet_result(outcome.categories, "Categorias"),
        vehicles=_sheet_result(outcome.vehicles, "Vehiculos"),
        attributes=_sheet_result(outcome.attributes, "Atributos"),
        variants=_sheet_result(outcome.variants, "Variantes"),
    )

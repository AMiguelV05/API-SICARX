from __future__ import annotations
from typing import List, Literal, Optional
from app.schemas.base import CamelModel

# Admin (/v1/admin/bulk-import/*, ver CLAUDE.md "Compatibilidad de vehiculos"/"Admin API") -
# carga masiva ADITIVA de product_categories/product_vehicles desde un .xlsx, en vez de una
# asignacion a la vez via PUT .../{uuid}/products o POST /admin/vehicles/assign-by-model.
# A diferencia de ambos, resuelve productos por sku (+ additional_skus como fallback), no
# por sicar_uuid - el admin llenando el Excel no conoce (ni deberia necesitar conocer) los
# uuids internos.

SheetName = Literal["Categorias", "Vehiculos"]

ReasonCode = Literal[
    "MISSING_FIELDS",
    "SKU_NOT_FOUND",
    "CATEGORY_SLUG_NOT_FOUND",
    "INVALID_YEAR",
    "VEHICLE_NOT_FOUND",
]


class BulkImportRowError(CamelModel):
    sheet: SheetName
    row: int  # numero de fila real en el archivo Excel (1-indexed; fila 2 = primera fila de datos)
    reason_code: ReasonCode
    reason: str  # mensaje en espanol, listo para mostrarse tal cual en el dashboard
    sku: Optional[str] = None


class BulkImportSheetResult(CamelModel):
    found: bool  # si la hoja existia en el archivo - distingue "hoja ausente" de "hoja presente pero con 0 filas"
    processed_rows: int
    assigned_count: int  # vinculos NUEVOS realmente insertados - ver ON CONFLICT DO NOTHING en bulk_import_service
    errors: List[BulkImportRowError] = []


class BulkImportProductsResponse(CamelModel):
    categories: BulkImportSheetResult
    vehicles: BulkImportSheetResult

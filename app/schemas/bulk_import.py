from __future__ import annotations
from typing import List, Literal, Optional
from app.schemas.base import CamelModel

# Carga masiva desde .xlsx; resuelve productos por sku (+ additional_skus), no por sicar_uuid - el admin no conoce los uuids internos.
# Categorias/Vehiculos son ADITIVOS (ON CONFLICT DO NOTHING); Atributos hace MERGE (una corrida posterior con un valor corregido SI se aplica);
# Variantes REEMPLAZA (variant_group_uuid es un solo valor por producto, no un tag) - ver bulk_import_service.import_bulk_assignments.

SheetName = Literal["Categorias", "Vehiculos", "Atributos", "Variantes"]

ReasonCode = Literal[
    "MISSING_FIELDS",
    "SKU_NOT_FOUND",
    "CATEGORY_SLUG_NOT_FOUND",
    "INVALID_YEAR",
    "VEHICLE_NOT_FOUND",
    "ATTRIBUTE_SLUG_NOT_FOUND",
    "VALUE_TYPE_MISMATCH",
    "VARIANT_GROUP_SLUG_NOT_FOUND",
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
    attributes: BulkImportSheetResult
    variants: BulkImportSheetResult

import logging
from typing import Optional
from fastapi import APIRouter, Depends, Body, Query, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.attribute import (
    AttributePresetPublic,
    AttributePresetCreateRequest,
    AttributePresetUpdateRequest,
    AttributePresetListResponse,
    ReplaceAttributePresetItemsRequest,
    AttributePresetItemsResponse,
    AttributePresetItemPublic,
    ApplyPresetRequest,
    ApplyPresetResponse,
)
from app.services import attribute_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/attribute-presets", tags=["Admin - Attribute Presets"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=AttributePresetPublic, status_code=status.HTTP_201_CREATED, summary="Crear un preset de atributos")
async def admin_create_preset(db: DbDep, data: AttributePresetCreateRequest = Body()):
    """Crea un bundle nombrado y reusable de atributos (p. ej. "Llantas") - conveniencia de
    captura, nunca obligatorio ni validado contra un producto."""
    preset = await attribute_service.create_preset(db, data.name)
    return AttributePresetPublic.model_validate(preset)


@router.get("", response_model=AttributePresetListResponse, summary="Buscar/listar presets de atributos")
async def admin_list_presets(
    db: DbDep,
    search: Optional[str] = Query(default=None, description="Coincidencia parcial contra name, sin distinguir mayusculas"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    total, presets = await attribute_service.list_presets(db, search=search, limit=limit, offset=offset)
    return AttributePresetListResponse(total=total, docs=[AttributePresetPublic.model_validate(p) for p in presets])


@router.patch("/{preset_uuid}", response_model=AttributePresetPublic, summary="Actualizacion parcial de un preset")
async def admin_update_preset(preset_uuid: str, db: DbDep, data: AttributePresetUpdateRequest = Body()):
    preset = await attribute_service.update_preset(db, preset_uuid, data.model_dump(exclude_unset=True))
    return AttributePresetPublic.model_validate(preset)


@router.delete("/{preset_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un preset (nunca bloqueado - no gatea nada de un producto)")
async def admin_delete_preset(preset_uuid: str, db: DbDep):
    """Borrado real, siempre permitido - a diferencia de atributos/grupos de variantes, un
    preset no gatea ni valida nada de un producto, solo es un atajo de captura."""
    await attribute_service.delete_preset(db, preset_uuid)


@router.put("/{preset_uuid}/attributes", response_model=AttributePresetItemsResponse, summary="Reemplazar el conjunto completo de atributos de un preset")
async def admin_replace_preset_attributes(preset_uuid: str, db: DbDep, data: ReplaceAttributePresetItemsRequest = Body()):
    """Reemplaza el conjunto COMPLETO de atributos del preset (no incremental). `404` si
    algun `attributeUuid` no resuelve."""
    await attribute_service.replace_preset_items(db, preset_uuid, [item.model_dump() for item in data.items])
    items = await attribute_service.list_preset_items(db, preset_uuid)
    return AttributePresetItemsResponse(preset_uuid=preset_uuid, docs=[AttributePresetItemPublic.model_validate(item) for item in items])


@router.get("/{preset_uuid}/attributes", response_model=AttributePresetItemsResponse, summary="Listar los atributos asignados a un preset")
async def admin_list_preset_attributes(preset_uuid: str, db: DbDep):
    """Pensada para precargar la UI de edicion antes de un PUT de arriba."""
    items = await attribute_service.list_preset_items(db, preset_uuid)
    return AttributePresetItemsResponse(preset_uuid=preset_uuid, docs=[AttributePresetItemPublic.model_validate(item) for item in items])


@router.post("/{preset_uuid}/apply", response_model=ApplyPresetResponse, summary="Aplicar (bulk-scaffold) los atributos de un preset a un lote de productos")
async def admin_apply_preset(preset_uuid: str, db: DbDep, data: ApplyPresetRequest = Body()):
    """Agrega las claves de atributos del preset a cada producto con valor `null`, SOLO
    para las claves que el producto todavia no tiene - un valor ya guardado nunca se
    sobreescribe. Pensado para bulk-scaffold "estos productos necesitan un campo color/
    talla" antes de llenar los valores reales (edicion por producto o el .xlsx de bulk-
    import)."""
    product_uuids, scaffolded_count = await attribute_service.apply_preset_to_products(db, preset_uuid, data.product_uuids)
    return ApplyPresetResponse(preset_uuid=preset_uuid, product_uuids=product_uuids, scaffolded_count=scaffolded_count)

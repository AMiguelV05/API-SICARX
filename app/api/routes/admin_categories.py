import logging
from typing import List, Optional
from fastapi import APIRouter, Depends, Body, Query, Response, status
from app.core.database import DbDep
from app.core.security import validate_admin_key
from app.schemas.taxonomy import (
    CategoryAdminPublic,
    CategoryCreateRequest,
    CategoryUpdateRequest,
    ReplaceCategoryProductsRequest,
    ReplaceCategoryProductsResponse,
    PatchCategoryProductsRequest,
    PatchCategoryProductsResponse,
    CategoryProductsResponse,
)
from app.schemas.products import ProductAdminBasic
from app.services import taxonomy_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/categories", tags=["Admin - Taxonomy"], dependencies=[Depends(validate_admin_key)])


@router.post("", response_model=CategoryAdminPublic, status_code=status.HTTP_201_CREATED, summary="Crear un nodo de categoria")
async def admin_create_category(db: DbDep, data: CategoryCreateRequest = Body()):
    """Crea un nodo del arbol de categorias. `slug` siempre se deriva de `name` (nunca lo
    manda el admin), desambiguado contra los slugs actuales. `parentUuid` omitido crea un
    nodo raiz; `404` si se manda uno que no existe."""
    category = await taxonomy_service.create_category(db, data.name, data.parent_uuid)
    return CategoryAdminPublic.model_validate(category)


@router.get("/by-product/{product_uuid}", response_model=List[CategoryAdminPublic], summary="Listar las categorias asignadas directamente a un producto")
async def admin_list_product_categories(product_uuid: str, db: DbDep):
    """Direccion inversa: dado un producto, sus categorias asignadas directamente (no
    incluye ancestros). Pensado para precargar una pantalla de edicion de tags. Lista sin
    paginar (acotada por naturaleza); `[]` si el producto existe pero no tiene ninguna."""
    categories = await taxonomy_service.get_categories_for_product(db, product_uuid)
    return [CategoryAdminPublic.model_validate(c) for c in categories]


@router.get("/export", summary="Descargar un CSV con todas las categorias y sus productos asignados")
async def admin_export_categories(
    db: DbDep,
    category_uuid: Optional[str] = Query(default=None, alias="taxonomyUuid", description="Acota el export a este nodo (arbol PIM de categorias) y sus descendientes; omitido exporta el arbol completo. Nombrado `taxonomyUuid` (no `categoryUuid`) para no chocar con Product.category_uuid, el campo crudo sincronizado de Sicar X que /products y /search filtran bajo ese otro nombre - ver CLAUDE.md."),
):
    """Un CSV con una fila por par (categoria, producto) - via outer joins, una categoria
    sin productos (o con productos ya eliminados) igual aparece con las columnas de
    producto en blanco. `taxonomyUuid` opcional acota el export a ese subarbol; `404` si
    no resuelve a una categoria real. Este endpoint es admin-only (no forma parte del
    contrato con el frontend), asi que este parametro se pudo renombrar directo sin capa
    de compatibilidad - a diferencia de los campos de OrderPublic/OrderResponse."""
    file_bytes = await taxonomy_service.export_categories_csv(db, category_uuid)
    return Response(
        content=file_bytes,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=categorias_productos.csv"},
    )


@router.patch("/{category_uuid}", response_model=CategoryAdminPublic, summary="Renombrar y/o mover (reasignar padre) un nodo de categoria")
async def admin_update_category(category_uuid: str, db: DbDep, data: CategoryUpdateRequest = Body()):
    """Actualizacion parcial (`exclude_unset`): renombrar y/o mover (reasignar
    `parentUuid`) en la misma llamada, no hay endpoint de "mover" separado. Renombrar
    recalcula el slug; mover valida que el nuevo padre no sea el nodo mismo ni uno de sus
    propios descendientes (`409` si lo es, guarda contra ciclos)."""
    category = await taxonomy_service.update_category(db, category_uuid, data.model_dump(exclude_unset=True))
    return CategoryAdminPublic.model_validate(category)


@router.delete("/{category_uuid}", status_code=status.HTTP_204_NO_CONTENT, summary="Eliminar un nodo de categoria (bloqueado si tiene subcategorias o productos asignados)")
async def admin_delete_category(category_uuid: str, db: DbDep):
    """Borrado real (no soft-delete). `409` si el nodo todavia tiene subcategorias o
    productos asignados en `product_categories` - hay que reasignarlos/quitarlos primero,
    deliberadamente sin cascada ni reparenteo automatico."""
    await taxonomy_service.delete_category(db, category_uuid)


@router.put("/{category_uuid}/products", response_model=ReplaceCategoryProductsResponse, summary="Reemplazar el conjunto completo de productos asignados a una categoria")
async def admin_replace_category_products(category_uuid: str, db: DbDep, data: ReplaceCategoryProductsRequest = Body()):
    """Reemplaza el conjunto COMPLETO de productos asignados directamente a la categoria
    (`productUuids`, resueltos contra `Product.sicar_uuid`) - unica forma de poblar
    `product_categories`. Reemplazo, no incremental - para categorias con mas productos
    asignados que el limite de `GET .../products` (200), preferir el `PATCH` de abajo para
    no arriesgar sobreescribir asignaciones que el picker del dashboard nunca cargo.
    `404` si algun uuid no resuelve."""
    product_uuids = await taxonomy_service.replace_category_products(db, category_uuid, data.product_uuids)
    return ReplaceCategoryProductsResponse(category_uuid=category_uuid, product_uuids=product_uuids)


@router.patch("/{category_uuid}/products", response_model=PatchCategoryProductsResponse, summary="Agregar/quitar productos de una categoria de forma incremental")
async def admin_patch_category_products(category_uuid: str, db: DbDep, data: PatchCategoryProductsRequest = Body()):
    """Incremental (a diferencia del `PUT` de arriba): agrega/quita sin necesitar conocer
    el conjunto completo asignado - seguro de usar aunque la categoria tenga mas productos
    que el limite de `GET .../products`. `add` ya asignados se ignoran (idempotente);
    `remove` no asignados o inexistentes tambien se ignoran (no-op tolerante). `422` si un
    mismo `productUuid` aparece en `add` y `remove` a la vez. `404` si algun uuid de `add`
    no resuelve a un producto real y no eliminado."""
    added, removed, added_count, removed_count = await taxonomy_service.patch_category_products(
        db, category_uuid, data.add, data.remove
    )
    return PatchCategoryProductsResponse(
        category_uuid=category_uuid, added=added, removed=removed, added_count=added_count, removed_count=removed_count
    )


@router.get("/{category_uuid}/products", response_model=CategoryProductsResponse, summary="Listar productos asignados directamente a una categoria (sin incluir descendientes)")
async def admin_list_category_products(
    category_uuid: str,
    db: DbDep,
    limit: int = Query(default=60, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Lista paginada de productos asignados directamente al nodo (sin descendientes -
    para eso esta el filtro `taxonomyUuid` de `/catalog`/`/search`). Pensada para poblar
    la UI de edicion antes de un PUT de arriba."""
    total, products = await taxonomy_service.list_category_products(db, category_uuid, limit, offset)
    return CategoryProductsResponse(total=total, docs=[ProductAdminBasic.model_validate(p) for p in products])

from typing import List
from app.schemas.base import CamelModel

class CategoryBasic(CamelModel):
    uuid: str
    name: str

class DepartmentWithCategories(CamelModel):
    uuid: str
    name: str
    order: int
    categories: List[CategoryBasic]

class TaxonomyResponse(CamelModel):
    departments: List[DepartmentWithCategories]

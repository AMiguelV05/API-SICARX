from pydantic import BaseModel
from typing import List

class CategoryBasic(BaseModel):
    uuid: str
    name: str

    class Config:
        from_attributes = True

class DepartmentWithCategories(BaseModel):
    uuid: str
    name: str
    order: int
    categories: List[CategoryBasic]

class TaxonomyResponse(BaseModel):
    departments: List[DepartmentWithCategories]

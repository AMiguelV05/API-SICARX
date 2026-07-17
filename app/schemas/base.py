from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base compartida: expone camelCase en el JSON de entrada/salida (alias_generator)
    mientras el codigo Python interno sigue usando snake_case (populate_by_name=True permite
    ambos). No requiere cambios en la capa de servicios: model_dump() sin by_alias=True sigue
    devolviendo claves snake_case."""
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)

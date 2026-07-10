"""Validación de identificadores usados en las consultas GraphQL/REST a Sicar X.

Los "uuid" de Sicar X no siempre son UUIDs RFC 4122 (los productos usan
identificadores alfanuméricos cortos tipo "3CmqnrZ1s0UiUjfbdRZVEg5bk5b",
mientras que departamentos/categorías/listas de precios sí usan el formato
estándar con guiones). Para evitar inyección en las queries GraphQL armadas
con f-strings, en lugar de exigir el formato UUID exacto, restringimos el
valor a un charset seguro que no puede romper un literal de cadena GraphQL.
"""
import re

_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_sicar_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID_PATTERN.fullmatch(value))

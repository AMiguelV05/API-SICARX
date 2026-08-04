"""Validación de identificadores usados en las consultas GraphQL/REST a Sicar X.

Los "uuid" de Sicar X no siempre son UUIDs RFC 4122 (p. ej. productos usan ids
alfanuméricos cortos), asi que en vez de exigir ese formato, se restringe el
valor a un charset seguro para evitar inyección en las queries GraphQL armadas
con f-strings.
"""
import re

_SAFE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def is_safe_sicar_id(value: str) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID_PATTERN.fullmatch(value))

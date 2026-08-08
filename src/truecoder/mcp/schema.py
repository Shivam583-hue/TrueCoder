from __future__ import annotations

from typing import Any, Final

MAX_SCHEMA_DEPTH: Final = 8
MAX_PROPERTIES: Final = 64
MAX_ENUM_VALUES: Final = 64
MAX_STRING_LENGTH: Final = 4096
MAX_DESCRIPTION_LENGTH: Final = 1024
MAX_NAME_LENGTH: Final = 128

ALLOWED_TYPES: Final = frozenset(
    {"object", "array", "string", "number", "integer", "boolean", "null"}
)

_SCALAR_KEYWORDS: Final = (
    "type",
    "format",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
)


class SchemaRejected(ValueError):
    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(message)


def bound_text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    collapsed = " ".join(value.split())
    if len(collapsed) > limit:
        return f"{collapsed[: limit - 1]}…"
    return collapsed


def bound_schema(schema: object, *, depth: int = 0) -> dict[str, Any]:
    if depth > MAX_SCHEMA_DEPTH:
        raise SchemaRejected("the schema is nested more deeply than allowed")
    if not isinstance(schema, dict):
        raise SchemaRejected("a schema must be a JSON object")

    bounded: dict[str, Any] = {}

    declared = schema.get("type")
    if isinstance(declared, str):
        if declared not in ALLOWED_TYPES:
            raise SchemaRejected(f"unsupported schema type {declared!r}")
        bounded["type"] = declared
    elif isinstance(declared, list):
        allowed = [item for item in declared if item in ALLOWED_TYPES]
        if not allowed:
            raise SchemaRejected("no supported type in the schema")
        bounded["type"] = allowed

    for keyword in _SCALAR_KEYWORDS:
        if keyword == "type" or keyword not in schema:
            continue
        value = schema[keyword]
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        if isinstance(value, str) and len(value) > MAX_STRING_LENGTH:
            continue
        bounded[keyword] = value

    description = bound_text(schema.get("description"), MAX_DESCRIPTION_LENGTH)
    if description:
        bounded["description"] = description

    enum = schema.get("enum")
    if isinstance(enum, list):
        if len(enum) > MAX_ENUM_VALUES:
            raise SchemaRejected("the schema lists more enum values than allowed")
        bounded["enum"] = [
            value
            for value in enum
            if isinstance(value, (str, int, float, bool)) or value is None
        ]

    properties = schema.get("properties")
    if isinstance(properties, dict):
        if len(properties) > MAX_PROPERTIES:
            raise SchemaRejected("the schema declares more properties than allowed")
        bounded_properties: dict[str, Any] = {}
        for name, child in properties.items():
            if not isinstance(name, str) or len(name) > MAX_NAME_LENGTH:
                raise SchemaRejected("a property name is unusable")
            bounded_properties[name] = bound_schema(child, depth=depth + 1)
        bounded["properties"] = bounded_properties

    items = schema.get("items")
    if isinstance(items, dict):
        bounded["items"] = bound_schema(items, depth=depth + 1)

    required = schema.get("required")
    if isinstance(required, list):
        names = [name for name in required if isinstance(name, str)]
        declared_properties = bounded.get("properties", {})
        if declared_properties:
            names = [name for name in names if name in declared_properties]
        bounded["required"] = names

    if bounded.get("type") == "object" and "properties" not in bounded:
        bounded["properties"] = {}

    if "type" not in bounded:
        bounded["type"] = "object"
        bounded.setdefault("properties", {})

    bounded["additionalProperties"] = False
    return bounded


def bound_tool_schema(schema: object) -> dict[str, Any]:
    bounded = bound_schema(schema)
    if bounded.get("type") != "object":
        raise SchemaRejected("a tool schema must describe a JSON object")
    return bounded

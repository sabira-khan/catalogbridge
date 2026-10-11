from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

JSONSchema = dict[str, Any]
STRIPPED_KEYWORDS = ("default",)


def strict_json_schema(model: type[BaseModel]) -> JSONSchema:
    schema: JSONSchema = model.model_json_schema()
    _strictify(schema)
    return schema


def response_format_for(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": model.__name__,
            "strict": True,
            "schema": strict_json_schema(model),
        },
    }


def schema_hint(model: type[BaseModel]) -> str:
    return json.dumps(strict_json_schema(model), indent=2, sort_keys=True)


def _strictify(node: object) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if isinstance(properties, dict):
            node["additionalProperties"] = False
            node["required"] = list(properties)
            for child in properties.values():
                _strictify(child)
        for keyword in STRIPPED_KEYWORDS:
            node.pop(keyword, None)
        for key, value in node.items():
            if key != "properties":
                _strictify(value)
    elif isinstance(node, list):
        for item in node:
            _strictify(item)

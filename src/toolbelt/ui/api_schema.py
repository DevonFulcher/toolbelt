"""The API's JSON Schema and state catalog as the files the web client is
generated from (``web/scripts/gen-types.mjs`` runs this).

``python -m toolbelt.ui.api_schema schema`` prints the JSON Schema of every
wire type; ``... catalog`` prints the state catalog exactly as the snapshot
serves it (the client's tests run against it). Both are committed under
``web/src/api/`` and a test fails when they are stale.
"""

import json
import sys

from pydantic.json_schema import GenerateJsonSchema

from toolbelt.ui import api_models


class _PlainSchema(GenerateJsonSchema):
    """No ``title`` on every field: the TypeScript generator would turn each
    into a named type (``Dimension14``); only the models and aliases are
    named."""

    def field_title_should_be_set(self, schema) -> bool:
        return False


def schema_json() -> str:
    schema = api_models.Wire.model_json_schema(
        mode="serialization", schema_generator=_PlainSchema
    )
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def catalog_json() -> str:
    data = api_models.build_catalog().model_dump(mode="json")
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


_OUTPUTS = {"schema": schema_json, "catalog": catalog_json}

if __name__ == "__main__":
    sys.stdout.write(_OUTPUTS[sys.argv[1]]())

"""Offline write contracts derived from integrity-pinned official Azure Swagger.

This intentionally implements only the schema vocabulary used by the four
pinned request bodies. Unknown schema constructs fail closed, never become
permissive validation. No external reference or network resolver is available.
"""

import argparse
import copy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import posixpath
import re
from urllib.parse import parse_qsl, urlsplit


API_VERSION = "2024-03-01"
SPEC_COMMIT = "1249f8e2b407b339083c5125a245bdd3057f6cd5"
PROVENANCE_SHA256 = "f32d30dc6b96aa0b5cab3a179d0645557702ae6930716493feca57e799875cfb"
ASSETS = Path(__file__).with_name("azure_write_schemas")
SPEC_DIRECTORY = "specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2024-03-01/"
OPERATIONS = {
    ("containerApps", "PATCH"): ("ContainerApps.json", "ContainerApps_Update"),
    ("containerApps", "PUT"): ("ContainerApps.json", "ContainerApps_CreateOrUpdate"),
    ("jobs", "PATCH"): ("Jobs.json", "Jobs_Update"),
    ("jobs", "PUT"): ("Jobs.json", "Jobs_CreateOrUpdate"),
}
_ANNOTATIONS = {"description", "title", "default", "example", "externalDocs"}
_EXTENSIONS = {
    "x-ms-azure-resource", "x-ms-client-flatten", "x-ms-enum",
    "x-ms-identifier", "x-ms-identifiers", "x-ms-mutability", "x-ms-secret", "x-nullable",
}
_KEYWORDS = {
    "type", "properties", "additionalProperties", "items", "required", "enum",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "uniqueItems",
    "minProperties", "maxProperties", "format", "readOnly",
}
_FORMATS = {"int32", "int64", "float", "double", "password", "arm-id"}
GENERATED_FILE = "generated-write-contracts.json"


class WriteSchemaError(ValueError):
    def __init__(self, reason):
        allowed = _KEYWORDS | {
            "integrity", "schema", "operation", "target", "body", "arguments",
            "dependency", "readOnly", "location",
        }
        self.reason = reason if reason in allowed else "schema"
        super().__init__("Unsupported Azure write request for API 2024-03-01; contract reason=" + self.reason)


def _require(condition, reason):
    if not condition:
        raise WriteSchemaError(reason)


def _json_object_pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "body")
        result[key] = value
    return result


@lru_cache(maxsize=1)
def _documents():
    try:
        raw = (ASSETS / "provenance.json").read_bytes()
        _require(hashlib.sha256(raw).hexdigest() == PROVENANCE_SHA256, "integrity")
        provenance = json.loads(raw)
        _require(provenance["commit"] == SPEC_COMMIT and provenance["api_version"] == API_VERSION, "integrity")
        documents = {}
        for name, digest in provenance["files"].items():
            candidate = ASSETS / "vendor" / name
            _require(candidate.resolve().is_relative_to((ASSETS / "vendor").resolve()), "integrity")
            data = candidate.read_bytes()
            _require(hashlib.sha256(data).hexdigest() == digest, "integrity")
            if name.endswith(".json"):
                documents[name] = json.loads(data)
        return documents
    except (OSError, TypeError, KeyError, ValueError):
        raise WriteSchemaError("integrity") from None


def _dereference(schema, filename, stack=()):
    _require(isinstance(schema, dict), "schema")
    if "$ref" in schema:
        ref = schema["$ref"]
        _require(isinstance(ref, str) and ref.count("#") == 1, "schema")
        external, pointer = ref.split("#")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(filename), external)) if external else filename
        key = (target, pointer)
        _require(key not in stack and target in _documents() and pointer.startswith("/definitions/"), "schema")
        value = _documents()[target]
        for part in pointer[1:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
        resolved = _dereference(value, target, (*stack, key))
        siblings = {name: value for name, value in schema.items() if name != "$ref"}
        _require(all(name in _ANNOTATIONS | _EXTENSIONS | {"readOnly", "type"}
                     for name in siblings), "schema")
        if "type" in siblings:
            _require(siblings["type"] == resolved.get("type", "object" if "properties" in resolved else None), "schema")
        return {**resolved, **siblings}
    result = {}
    for name, value in schema.items():
        if name == "properties":
            result[name] = {key: _dereference(child, filename, stack) for key, child in value.items()}
        elif name in ("items", "additionalProperties") and isinstance(value, dict):
            result[name] = _dereference(value, filename, stack)
        elif name == "allOf":
            result[name] = [_dereference(child, filename, stack) for child in value]
        else:
            result[name] = copy.deepcopy(value)
    return result


def _flatten(schema):
    result = {}
    for part in [*schema.get("allOf", []), {key: value for key, value in schema.items() if key != "allOf"}]:
        flattened = _flatten(part) if "allOf" in part else part
        for key, value in flattened.items():
            if key == "properties":
                properties = result.setdefault(key, {})
                _require(all(name not in properties or properties[name] == child for name, child in value.items()), "schema")
                properties.update(value)
            elif key == "required":
                result[key] = sorted(set(result.get(key, [])) | set(value))
            else:
                _require(key not in result or result[key] == value or key in _ANNOTATIONS | _EXTENSIONS, "schema")
                result[key] = value
    return result


def _audit_vocabulary(schema):
    _require(isinstance(schema, dict)
             and set(schema) <= _KEYWORDS | _ANNOTATIONS | _EXTENSIONS | {"allOf"}, "schema")
    for child in schema.get("properties", {}).values():
        _audit_vocabulary(child)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            _audit_vocabulary(schema[key])
    for child in schema.get("allOf", []):
        _audit_vocabulary(child)


def _compile(schema):
    _audit_vocabulary(schema)
    schema = _flatten(schema)
    if schema.get("readOnly"):
        return {"readOnly": True, "not": {}}
    _require(schema.get("format") is None or schema["format"] in _FORMATS, "schema")
    _require(schema.get("type") in (None, "object", "array", "string", "integer", "number", "boolean"), "schema")
    result = {key: copy.deepcopy(value) for key, value in schema.items()
              if key in _KEYWORDS - {"properties", "items", "additionalProperties"}}
    if "properties" in schema or "additionalProperties" in schema or schema.get("type") == "object":
        result["type"] = "object"
        result["properties"] = {key: _compile(child) for key, child in schema.get("properties", {}).items()}
        if "required" in result:
            result["required"] = [name for name in result["required"]
                                  if not result["properties"][name].get("readOnly")]
        additional = schema.get("additionalProperties", False)
        result["additionalProperties"] = _compile(additional) if isinstance(additional, dict) else additional
        _require(isinstance(result["additionalProperties"], (bool, dict)), "schema")
    if "items" in schema:
        result["items"] = _compile(schema["items"])
    if schema.get("format") in {"int32", "int64"}:
        bits = int(schema["format"][3:])
        result["minimum"] = max(result.get("minimum", -(2 ** (bits - 1))), -(2 ** (bits - 1)))
        result["maximum"] = min(result.get("maximum", 2 ** (bits - 1) - 1), 2 ** (bits - 1) - 1)
    elif schema.get("format") in {"float", "double"}:
        maximum = float.fromhex("0x1.fffffep+127" if schema["format"] == "float" else "0x1.fffffffffffffp+1023")
        result["minimum"] = max(result.get("minimum", -maximum), -maximum)
        result["maximum"] = min(result.get("maximum", maximum), maximum)
    if schema.get("x-nullable") is True:
        _require(isinstance(result.get("type"), str), "schema")
        result["x-nullable"] = True
        result["type"] = [result["type"], "null"]
        if "enum" in result and None not in result["enum"]:
            result["enum"].append(None)
    # x-ms-mutability is a code-generation annotation, not a wire-schema change.
    return result


def _source_contract(kind, method):
    _require((kind, method) in OPERATIONS, "operation")
    filename, operation_id = OPERATIONS[kind, method]
    filename = SPEC_DIRECTORY + filename
    document = _documents()[filename]
    _require(document["swagger"] == "2.0" and document["info"]["version"] == API_VERSION, "integrity")
    matches = [(path, operation) for path, methods in document["paths"].items()
               for verb, operation in methods.items()
               if verb == method.lower() and operation.get("operationId") == operation_id]
    _require(len(matches) == 1, "operation")
    route, operation = matches[0]
    bodies = [entry for entry in operation["parameters"] if entry.get("in") == "body"]
    _require(len(bodies) == 1 and bodies[0].get("required") is True, "schema")
    return route, _compile(_dereference(bodies[0]["schema"], filename))


def generate_contracts():
    """Regenerate all four write schemas using only verified official sources."""
    result = {
        "schema_version": 1, "api_version": API_VERSION, "source_commit": SPEC_COMMIT,
        "source_provenance_sha256": PROVENANCE_SHA256, "contracts": {},
    }
    for (kind, method), (_, operation_id) in OPERATIONS.items():
        route, body = _source_contract(kind, method)
        result["contracts"].setdefault(kind, {})[method] = {
            "operation_id": operation_id, "path": route,
            "schema": {"$schema": "http://json-schema.org/draft-04/schema#", **body},
        }
    return result


def check_generated():
    """Structural equality avoids treating JSON property ordering as provenance."""
    expected = generate_contracts()
    try:
        actual = json.loads((ASSETS / GENERATED_FILE).read_text(), object_pairs_hook=_json_object_pairs)
    except (OSError, ValueError):
        raise WriteSchemaError("integrity") from None
    _require(actual == expected, "integrity")
    return expected


@lru_cache(maxsize=4)
def _contract(kind, method):
    _require((kind, method) in OPERATIONS, "operation")
    value = check_generated()["contracts"][kind][method]
    return value["path"], value["schema"]


def request_schema(kind, method):
    return copy.deepcopy(_contract(kind, method)[1])


def _validate(schema, value):
    _require(not schema.get("readOnly"), "readOnly")
    if value is None and schema.get("x-nullable"):
        return
    expected = schema.get("type")
    matches = {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "integer": type(value) is int,
        "number": type(value) in (int, float) and (type(value) is int or math.isfinite(value)),
        "boolean": type(value) is bool,
    }
    kinds = expected if isinstance(expected, list) else [expected]
    _require(expected is None or any(matches.get(kind, False) for kind in kinds), "type")
    if "enum" in schema:
        _require(any(type(value) is type(candidate) and value == candidate for candidate in schema["enum"]), "enum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        _require(set(schema.get("required", [])) <= set(value), "required")
        for key, child in value.items():
            _require(isinstance(key, str), "type")
            child_schema = properties.get(key, schema.get("additionalProperties", False))
            _require(child_schema is not False, "additionalProperties")
            if child_schema is not True:
                _validate(child_schema, child)
        _require(len(value) >= schema.get("minProperties", 0), "minProperties")
        _require(len(value) <= schema.get("maxProperties", math.inf), "maxProperties")
    if isinstance(value, list):
        _require(len(value) >= schema.get("minItems", 0), "minItems")
        _require(len(value) <= schema.get("maxItems", math.inf), "maxItems")
        if schema.get("uniqueItems"):
            _require(len({json.dumps(item, sort_keys=True, allow_nan=False) for item in value}) == len(value), "uniqueItems")
        for item in value:
            _validate(schema["items"], item)
    if isinstance(value, str):
        _require(len(value) >= schema.get("minLength", 0), "minLength")
        _require(len(value) <= schema.get("maxLength", math.inf), "maxLength")
        if "pattern" in schema:
            _require(re.search(schema["pattern"], value) is not None, "pattern")
    if type(value) in (int, float):
        if schema.get("format") in ("int32", "int64"):
            bits = int(schema["format"][3:])
            _require(-(2 ** (bits - 1)) <= value < 2 ** (bits - 1), "format")
        for keyword, exclusive, comparison in (
            ("minimum", "exclusiveMinimum", lambda x, y: x > y),
            ("maximum", "exclusiveMaximum", lambda x, y: x < y),
        ):
            if keyword in schema:
                bound = schema[keyword]
                _require(comparison(value, bound) or (not schema.get(exclusive) and value == bound), keyword)
        if "multipleOf" in schema:
            _require(math.isclose(value / schema["multipleOf"], round(value / schema["multipleOf"])), "multipleOf")


def validate_payload(kind, method, payload):
    try:
        json.dumps(payload, allow_nan=False)
        _validate(_contract(kind, method)[1], payload)
    except WriteSchemaError:
        raise
    except (TypeError, ValueError, KeyError, OverflowError, RecursionError):
        raise WriteSchemaError("body") from None


def build_payload(kind, method="PATCH", **members):
    payload = copy.deepcopy(members)
    validate_payload(kind, method, payload)
    return payload


def project_component(kind, method, path, value):
    """Normalize captured GET components and validate the resulting write value."""
    schema = _contract(kind, method)[1]
    for name in path:
        schema = schema["properties"][name]

    def project(node, instance):
        if isinstance(instance, dict):
            result = {}
            for key, child in instance.items():
                child_schema = node.get("properties", {}).get(key, node.get("additionalProperties", False))
                _require(child_schema is not False, "additionalProperties")
                if isinstance(child_schema, dict):
                    if child_schema.get("readOnly"):
                        continue
                    if child is None and key not in node.get("required", []) and not child_schema.get("x-nullable"):
                        continue
                    result[key] = project(child_schema, child)
                else:
                    result[key] = copy.deepcopy(child)
            return result
        if isinstance(instance, list):
            return [project(node["items"], item) for item in instance]
        return copy.deepcopy(instance)

    result = project(schema, value)
    _validate(schema, result)
    return result


def target_kind(method, url):
    _require(method in {"PATCH", "PUT"}, "operation")
    try:
        parts = urlsplit(url)
        _require(parts.scheme == "https" and parts.hostname == "management.azure.com"
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.fragment and "%" not in parts.path
                 and parse_qsl(parts.query, keep_blank_values=True) == [("api-version", API_VERSION)], "target")
        for kind in ("containerApps", "jobs"):
            route = _contract(kind, method)[0]
            pattern = "".join("[A-Za-z0-9_.()-]+" if piece.startswith("{") else re.escape(piece)
                              for piece in re.split(r"(\{[^}]+\})", route))
            if re.fullmatch(pattern, parts.path, flags=re.IGNORECASE):
                return kind
    except WriteSchemaError:
        raise
    except (TypeError, ValueError):
        raise WriteSchemaError("target") from None
    raise WriteSchemaError("target")


def validate_request(method, url, payload):
    validate_payload(target_kind(method, url), method, payload)


def validate_azure_arguments(arguments):
    """Validate explicit az rest writes. Other PUT destinations are never exempt."""
    if arguments and arguments[0] == "containerapp":
        read_or_post = (
            ("containerapp", "show"), ("containerapp", "env", "show"),
            ("containerapp", "revision", "show"), ("containerapp", "job", "show"),
            ("containerapp", "job", "execution", "list"),
            ("containerapp", "job", "start"), ("containerapp", "job", "stop"),
        )
        _require(any(tuple(arguments[:len(prefix)]) == prefix for prefix in read_or_post), "operation")
        return
    if not arguments or arguments[0] != "rest":
        return
    aliases = {"--method": "method", "-m": "method", "--url": "url", "--uri": "url",
               "-u": "url", "--body": "body", "-b": "body"}
    options = {}
    other_flags = []
    index = 1
    while index < len(arguments):
        argument = arguments[index]
        flag, separator, inline = argument.partition("=")
        if flag in aliases:
            name = aliases[flag]
            _require(name not in options, "arguments")
            if separator:
                options[name] = inline
            else:
                index += 1
                _require(index < len(arguments), "arguments")
                options[name] = arguments[index]
        elif flag.startswith(("--m", "-m")):
            raise WriteSchemaError("arguments")
        elif flag.startswith("-"):
            other_flags.append(flag)
        index += 1
    method = options.get("method", "GET").upper()
    _require(method in {"PATCH", "PUT", "GET", "HEAD", "OPTIONS", "POST", "DELETE"}, "operation")
    if method not in {"PATCH", "PUT"}:
        return
    _require(set(other_flags) <= {"--headers", "--subscription", "--resource", "--query",
                                 "--only-show-errors", "--output", "-o"}, "arguments")
    try:
        kind = target_kind(method, options["url"])
        body = options["body"]
        text = Path(body[1:]).read_text() if body.startswith("@") else body
        payload = json.loads(text, object_pairs_hook=_json_object_pairs)
        validate_payload(kind, method, payload)
    except WriteSchemaError:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise WriteSchemaError("body") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-generated", action="store_true")
    action.add_argument("--write-generated", action="store_true")
    options = parser.parse_args()
    if options.write_generated:
        value = generate_contracts()
        (ASSETS / GENERATED_FILE).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        print("Regenerated four write contracts from verified pinned sources.")
    else:
        check_generated()
        print("Verified four generated write contracts and their official source pins.")


if __name__ == "__main__":
    main()

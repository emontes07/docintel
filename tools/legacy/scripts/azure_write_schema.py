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
ACTION_PROVENANCE_SHA256 = "d661e1322799eea642097bbc1a879958f39befe6e2776393f190bbdb4582de6f"
ASSETS = Path(__file__).with_name("azure_write_schemas")
SPEC_DIRECTORY = "specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2024-03-01/"
OPERATIONS = {
    ("containerApps", "PATCH"): ("ContainerApps.json", "ContainerApps_Update"),
    ("containerApps", "PUT"): ("ContainerApps.json", "ContainerApps_CreateOrUpdate"),
    ("jobs", "PATCH"): ("Jobs.json", "Jobs_Update"),
    ("jobs", "PUT"): ("Jobs.json", "Jobs_CreateOrUpdate"),
}
ACR_SPEC = "specification/containerregistry/resource-manager/Microsoft.ContainerRegistry/RegistryTasks/stable/2019-04-01/containerregistry_build.json"
JOB_ACTION_SPEC = "specification/app/resource-manager/Microsoft.App/ContainerApps/stable/2025-01-01/Jobs.json"
BLOB_SPEC = "specification/storage/data-plane/Microsoft.BlobStorage/stable/2024-08-04/blob.json"
ACTION_OPERATIONS = {
    "acrUpload": (ACR_SPEC, "2019-04-01", "Registries_GetBuildSourceUploadUrl"),
    "acrBuild": (ACR_SPEC, "2019-04-01", "Registries_ScheduleRun"),
    "jobStart": (JOB_ACTION_SPEC, "2025-01-01", "Jobs_Start"),
    "jobStart202507": (JOB_ACTION_SPEC.replace("2025-01-01", "2025-07-01"), "2025-07-01", "Jobs_Start"),
    "jobStop": (JOB_ACTION_SPEC, "2025-01-01", "Jobs_StopExecution"),
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
NATIVE_JOB_START_ACTIONS = {"2025-01-01": "jobStart", "2025-07-01": "jobStart202507"}


class WriteSchemaError(ValueError):
    def __init__(self, reason):
        allowed = _KEYWORDS | {
            "integrity", "schema", "operation", "target", "body", "arguments",
            "dependency", "readOnly", "location",
        }
        self.reason = reason if reason in allowed else "schema"
        super().__init__("Unsupported Azure write request for API 2024-03-01; contract reason=" + self.reason)


def native_cli_diagnostic(value):
    reasons = {"unsupported_job_api_version", "native_invocation_failed", "request_mismatch",
               "unexpected_request", "network_attempt"}
    result = {"reason": value.get("reason") if value.get("reason") in reasons else "native_invocation_failed"}
    for name, pattern in (("client_version", r"\d+\.\d+\.\d+"), ("job_api_version", r"\d{4}-\d{2}-\d{2}")):
        text = value.get(name)
        result[name] = text if isinstance(text, str) and re.fullmatch(pattern, text) else "unavailable"
    for name in ("captured_write_requests", "fixture_get_responses", "network_attempts", "unexpected_requests"):
        count = value.get(name)
        result[name] = count if type(count) is int and 0 <= count <= 100000 else 0
    return result


class NativeCLIError(ValueError):
    def __init__(self, **diagnostic):
        self.diagnostic = native_cli_diagnostic(diagnostic)
        super().__init__("Native CLI no-send rejected: " + json.dumps(self.diagnostic, sort_keys=True))


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
        documents = {}
        for manifest, pin in (("provenance.json", PROVENANCE_SHA256),
                              ("action-provenance.json", ACTION_PROVENANCE_SHA256)):
            raw = (ASSETS / manifest).read_bytes()
            _require(hashlib.sha256(raw).hexdigest() == pin, "integrity")
            provenance = json.loads(raw)
            _require(provenance["commit"] == SPEC_COMMIT, "integrity")
            if manifest == "provenance.json":
                _require(provenance["api_version"] == API_VERSION, "integrity")
            else:
                _require(set(provenance["api_versions"]) == {"2019-04-01", "2024-08-04", *NATIVE_JOB_START_ACTIONS}, "integrity")
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
        "source_provenance_sha256": PROVENANCE_SHA256,
        "action_source_provenance_sha256": ACTION_PROVENANCE_SHA256, "contracts": {}, "actions": {},
    }
    for (kind, method), (_, operation_id) in OPERATIONS.items():
        route, body = _source_contract(kind, method)
        result["contracts"].setdefault(kind, {})[method] = {
            "operation_id": operation_id, "path": route,
            "schema": {"$schema": "http://json-schema.org/draft-04/schema#", **body},
        }
    for action in ACTION_OPERATIONS:
        result["actions"][action] = _source_action(action)
    _require(result["actions"]["jobStart"]["schema"] == result["actions"]["jobStart202507"]["schema"], "schema")
    result["binary_upload"] = _source_blob()
    return result


def _source_blob():
    document = _documents()[BLOB_SPEC]
    path = document["x-ms-paths"]["/{containerName}/{blob}?BlockBlob"]
    operation = path["put"]
    _require(operation["operationId"] == "BlockBlob_Upload"
             and operation["consumes"] == ["application/octet-stream"], "schema")
    properties = {}
    for parameter in [*path["parameters"], *operation["parameters"]]:
        if "$ref" in parameter:
            _require(set(parameter) == {"$ref"} and parameter["$ref"].startswith("#/parameters/"), "schema")
            parameter = document["parameters"][parameter["$ref"].split("/")[-1]]
        if parameter.get("required") and parameter["in"] == "header":
            properties[parameter["name"].lower()] = _compile({
                key: value for key, value in parameter.items()
                if key not in {"name", "in", "required", "x-ms-client-name", "x-ms-parameter-location"}
            })
        if parameter["in"] == "body":
            _require(parameter["required"] and parameter["schema"] == {"type": "object", "format": "file"},
                     "schema")
    return {
        "operation_id": operation["operationId"], "api_version": document["info"]["version"],
        "body": {"type": "object", "format": "file"}, "content_type": operation["consumes"][0],
        "headers": {"type": "object", "properties": properties,
                    "required": sorted(properties), "additionalProperties": False},
    }


def validate_blob_request(url, headers, archive):
    parts = urlsplit(url)
    _require(parts.scheme == "https" and parts.hostname is not None
             and re.fullmatch(r"[a-z0-9]+\.blob\.core\.windows\.net", parts.hostname)
             and parts.port in (None, 443) and not parts.username and not parts.password
             and not parts.fragment and len(parts.path.split("/")) >= 3, "target")
    _require(not {"comp", "restype", "snapshot", "versionid"} &
             {key.lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)}, "target")
    contract = check_generated()["binary_upload"]
    normalized = {key.lower(): value for key, value in headers.items()}
    _require(normalized.pop("content-type", None) == contract["content_type"], "body")
    _validate(contract["headers"], normalized)
    _require(Path(archive).is_file() and Path(archive).stat().st_size == normalized["content-length"], "body")


def _source_action(action):
    filename, version, operation_id = ACTION_OPERATIONS[action]
    document = _documents()[filename]
    _require(document["swagger"] == "2.0" and document["info"]["version"] == version, "integrity")
    matches = [(route, methods["post"]) for route, methods in document["paths"].items()
               if methods.get("post", {}).get("operationId") == operation_id]
    _require(len(matches) == 1, "operation")
    route, operation = matches[0]
    bodies = [entry for entry in operation["parameters"] if entry.get("in") == "body"]
    _require(len(bodies) <= 1, "schema")
    body = _dereference(bodies[0]["schema"], filename) if bodies else None
    if action == "acrBuild":
        _require(body.get("discriminator") == "type", "schema")
        subtype = document["definitions"]["DockerBuildRequest"]
        _require(subtype.get("x-ms-discriminator-value") == "DockerBuildRequest"
                 and {"$ref": "#/definitions/RunRequest"} in subtype.get("allOf", []), "schema")
        body = _dereference(subtype, filename)
        def selected_variant(value):
            value = copy.deepcopy(value)
            discriminator = value.pop("discriminator", None)
            _require(discriminator in (None, "type"), "schema")
            marker = value.pop("x-ms-discriminator-value", None)
            _require(marker in (None, "DockerBuildRequest"), "schema")
            if "allOf" in value:
                value["allOf"] = [selected_variant(part) for part in value["allOf"]]
            return value
        body = _compile(selected_variant(body))
        body["properties"]["type"]["enum"] = ["DockerBuildRequest"]
    elif body is not None:
        body = _compile(body)
    return {"operation_id": operation_id, "api_version": version, "path": route, "schema": body}


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


@lru_cache(maxsize=5)
def action_contract(action):
    _require(action in ACTION_OPERATIONS, "operation")
    return copy.deepcopy(check_generated()["actions"][action])


def validate_action_payload(action, payload):
    contract = action_contract(action)
    if contract["schema"] is None:
        _require(payload is None, "body")
        return
    _validate(contract["schema"], payload)
    if action == "acrBuild":
        _require(payload.get("agentConfiguration") == {"cpu": 2} and payload.get("timeout") == 900,
                 "body")


def action_for_url(method, url):
    _require(method == "POST", "operation")
    try:
        parts = urlsplit(url)
        _require(parts.scheme == "https" and parts.hostname == "management.azure.com"
                 and parts.port in (None, 443) and not parts.username and not parts.password
                 and not parts.fragment and "%" not in parts.path, "target")
        for action in ACTION_OPERATIONS:
            contract = action_contract(action)
            pattern = "".join("[A-Za-z0-9_.()-]+" if piece.startswith("{") else re.escape(piece)
                              for piece in re.split(r"(\{[^}]+\})", contract["path"]))
            if (re.fullmatch(pattern, parts.path, flags=re.IGNORECASE)
                    and parse_qsl(parts.query, keep_blank_values=True)
                    == [("api-version", contract["api_version"])]):
                return action
    except WriteSchemaError:
        raise
    except (ValueError, TypeError):
        raise WriteSchemaError("target") from None
    raise WriteSchemaError("target")


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
    return _project_value(schema, value)


def project_action_payload(action, value):
    return _project_value(action_contract(action)["schema"], value)


def _project_value(schema, value):
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
        if isinstance(instance, str) and "enum" in node and instance not in node["enum"]:
            # CLI display casing can differ from the canonical write enum.
            matches = [value for value in node["enum"]
                       if isinstance(value, str) and value.casefold() == instance.casefold()]
            if len(matches) == 1:
                return matches[0]
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
    if method == "POST":
        validate_action_payload(action_for_url(method, url), payload)
    else:
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
        reads = (("account", "show"), ("account", "get-access-token"),
                 ("acr", "show"), ("acr", "repository", "show"),
                 ("ad", "app", "show"), ("ad", "sp", "show"),
                 ("ad", "signed-in-user", "show"), ("role", "assignment", "list"),
                 ("cognitiveservices", "account", "show"),
                 ("cognitiveservices", "account", "deployment", "show"),
                 ("storage", "account", "show"), ("storage", "blob", "list"))
        _require(any(tuple(arguments[:len(prefix)]) == prefix for prefix in reads), "operation")
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
    _require(method != "DELETE", "operation")
    if method not in {"PATCH", "PUT", "POST"}:
        return
    _require(set(other_flags) <= {"--headers", "--subscription", "--resource", "--query",
                                 "--only-show-errors", "--output", "-o"}, "arguments")
    try:
        if method == "POST":
            action_for_url(method, options["url"])
        else:
            target_kind(method, options["url"])
        body = options.get("body")
        text = Path(body[1:]).read_text() if body and body.startswith("@") else body
        payload = json.loads(text, object_pairs_hook=_json_object_pairs) if text is not None else None
        validate_request(method, options["url"], payload)
        return {"method": method, "url": options["url"], "body": payload}
    except WriteSchemaError:
        raise
    except (OSError, TypeError, ValueError, KeyError):
        raise WriteSchemaError("body") from None


def _wire_fingerprints(request):
    body = request.body
    body_digest = hashlib.sha256()
    request_digest = hashlib.sha256((request.method + "\n" + request.url + "\n").encode())
    size = 0
    position = None
    if body is None:
        chunks = ()
    elif isinstance(body, (str, bytes)):
        chunks = (body.encode() if isinstance(body, str) else body,)
    else:
        position = body.tell()
        chunks = iter(lambda: body.read(1024 * 1024), b"")
    try:
        for chunk in chunks:
            _require(isinstance(chunk, bytes), "body")
            size += len(chunk)
            body_digest.update(chunk)
            request_digest.update(chunk)
    finally:
        if position is not None:
            body.seek(position)
    return {
        "method": request.method,
        "api_version": dict(parse_qsl(urlsplit(request.url).query)).get("api-version")
                       or request.headers.get("x-ms-version"),
        "content_type": request.headers.get("Content-Type"),
        "endpoint_sha256": hashlib.sha256(request.url.encode()).hexdigest(),
        "body_sha256": body_digest.hexdigest(), "body_bytes": size,
        "request_sha256": request_digest.hexdigest(),
    }


def native_job_start_url(expected, api_version, client_version):
    action = NATIVE_JOB_START_ACTIONS.get(api_version)
    if action is None:
        raise NativeCLIError(reason="unsupported_job_api_version", client_version=client_version,
                             job_api_version=api_version)
    # Both pinned operation schemas are identical; no native coercion can rescue invalid input.
    validate_action_payload(action, expected["body"])
    parts = urlsplit(expected["url"])
    url = parts._replace(query="api-version=" + api_version).geturl()
    _require(action_for_url(expected["method"], url) == action, "target")
    return url


def native_cli_no_send(packet):
    """Run the installed CLI parser/client with isolated state and denied transports."""
    import contextlib
    import io
    import os
    import socket

    root = Path(packet["state_directory"])
    for key, directory in (("HOME", "home"), ("AZURE_CONFIG_DIR", "config"), ("AZURE_EXTENSION_DIR", "extensions")):
        target = root / directory
        target.mkdir(mode=0o700)
        os.environ[key] = str(target)
    os.environ.update(AZURE_CORE_COLLECT_TELEMETRY="no", AZURE_CORE_CHECK_VERSION="false",
                      AZURE_EXTENSION_USE_DYNAMIC_INSTALL="no", AZURE_CORE_NO_COLOR="true", TMPDIR=str(root))
    attempts = []
    def no_socket(*args, **kwargs):
        attempts.append("socket")
        raise RuntimeError("No-send transport boundary")
    socket.socket.connect = socket.socket.connect_ex = no_socket
    socket.socket.sendto = no_socket
    socket.create_connection = socket.getaddrinfo = no_socket

    from azure.cli.core import __version__, get_default_cli
    from azure.cli.core._profile import Profile
    from requests import Response, Session, __version__ as transport_version

    Profile.get_raw_token = lambda *a, **k: (
        ("Bearer", "NO-SEND-NOT-A-CREDENTIAL", {}), packet.get("subscription"), "no-send-tenant",
    )
    Profile.get_subscription = lambda *a, **k: {
        "id": packet.get("subscription"), "tenantId": "no-send-tenant",
        "name": "no-send", "state": "Enabled", "isDefault": True,
    }
    Profile.get_subscription_id = lambda *a, **k: packet.get("subscription")
    prepared = []
    auxiliary = []
    fixture_counts = {"get": 0, "write": 0}
    wire = []
    expected = copy.deepcopy(packet["expected"])
    observed = packet.get("resource_snapshot")
    job_api_version = None
    if tuple(packet["arguments"][:3]) == ("containerapp", "job", "start"):
        from azure.cli.command_modules.containerapp._clients import ContainerAppsJobClient
        job_api_version = ContainerAppsJobClient.api_version
        expected["url"] = native_job_start_url(expected, job_api_version, __version__)
    request_errors = []
    def capture(session, request, **kwargs):
        if request.method == expected["method"] and request.url == expected["url"]:
            try:
                actual = json.loads(request.body, object_pairs_hook=_json_object_pairs) if request.body else None
                validate_request(request.method, request.url, actual)
                _require(actual == expected["body"], "body")
                _require(request.headers.get("Content-Type", "").split(";")[0] == "application/json"
                         or actual is None, "body")
                for key, value in expected.get("headers", {}).items():
                    _require(request.headers.get(key) == value, "body")
            except (ValueError, TypeError):
                request_errors.append("request_mismatch")
                raise
            prepared.append({
                "method": request.method, "url_sha256": hashlib.sha256(request.url.encode()).hexdigest(),
                "body_sha256": hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest(),
                "body_hash_encoding": "canonical-json",
            })
            wire.append(_wire_fingerprints(request))
            fixture_counts["write"] += 1
            value = {}
        elif (request.method == "GET" and observed and job_api_version and request.url.lower() == (
                "https://management.azure.com" + observed["id"] + "?api-version=" + job_api_version).lower()):
            value = observed
            fixture_counts["get"] += 1
        elif request.method in {"GET", "HEAD"}:
            auxiliary.append("blocked_auxiliary_read")
            raise RuntimeError("No-send auxiliary read")
        else:
            attempts.append("unexpected_request")
            raise RuntimeError("No-send unexpected request")
        response = Response()
        response.status_code = 200
        response._content = json.dumps(value).encode()
        response.request = request
        response.headers = {"Content-Type": "application/json"}
        return response
    Session.send = capture
    output = io.StringIO()
    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
        cli = get_default_cli()
        result = cli.invoke(packet["arguments"], out_file=output)
    if result != 0 or len(prepared) != 1 or attempts or request_errors:
        reason = ("network_attempt" if "socket" in attempts else "unexpected_request" if attempts
                  else "request_mismatch" if request_errors else "native_invocation_failed")
        raise NativeCLIError(reason=reason, client_version=__version__, job_api_version=job_api_version,
                             captured_write_requests=len(prepared), fixture_get_responses=fixture_counts["get"],
                             network_attempts=attempts.count("socket"), unexpected_requests=attempts.count("unexpected_request"))
    return {
        "schema_version": 1, "status": "validated", "no_send": True, "client": "azure-cli",
        "client_version": __version__, "request": prepared[0], "network_requests_sent": 0,
        "auxiliary_reads_blocked": len(auxiliary),
        "validation_status": "validated_no_send", "provider": "deployment",
        "sdk_package": "azure-cli", "sdk_version": __version__,
        "transport_package": "requests", "transport_version": transport_version,
        "transport": "intercepted_prepared_request_with_fixture_responses",
        "transport_real_calls": 0, "network_calls": 0, "credential_real_calls": 0,
        "captured_requests": 1, "authentication_performed": False, "provider_send_performed": False,
        "provider_response_fabricated": True, "fixture_response_counts": fixture_counts,
        **wire[0],
    }


def native_http_request(packet, *, send=False):
    """Two explicit transports: pinned binary Blob upload and existing job-secret PATCH."""
    import contextlib
    import socket

    if not send:
        def denied(*args, **kwargs):
            raise WriteSchemaError("transport")
        socket.socket.connect = socket.socket.connect_ex = denied
        socket.socket.sendto = denied
        socket.create_connection = socket.getaddrinfo = denied
    from requests import Request, Session, __version__
    from requests.adapters import HTTPAdapter
    timeout = packet["timeout"]
    _require(type(timeout) in (float, int) and 0 < timeout < float("inf"), "arguments")
    method, url, headers = packet["method"], packet["url"], packet["headers"]
    with contextlib.ExitStack() as stack:
        if packet["contract"] == "binary_blob":
            _require(method == "PUT", "operation")
            validate_blob_request(url, headers, packet["archive"])
            data = stack.enter_context(open(packet["archive"], "rb"))
        elif packet["contract"] == "job_secret":
            _require(method == "PATCH" and target_kind(method, url) == "jobs", "operation")
            _require(set(headers) <= {"Content-Type", "Authorization", "If-Match"}, "arguments")
            validate_request(method, url, packet["payload"])
            data = json.dumps(packet["payload"], separators=(",", ":")).encode()
        else:
            raise WriteSchemaError("operation")
        session = stack.enter_context(Session())
        session.trust_env = False
        session.mount("https://", HTTPAdapter(max_retries=0))
        prepared = session.prepare_request(Request(method, url, headers={k: str(v) for k, v in headers.items()}, data=data))
        _require(prepared.url == url and prepared.method == method, "target")
        for key, value in headers.items():
            _require(prepared.headers.get(key) == str(value), "body")
        if packet["contract"] == "binary_blob":
            _require(prepared.body is data and prepared.headers["Content-Length"] == str(Path(packet["archive"]).stat().st_size),
                     "body")
        else:
            _require(prepared.body == data and prepared.headers["Content-Type"] == "application/json", "body")
        receipt = {"status": "validated", "no_send": not send, "client": "requests",
                   "client_version": __version__, "method": method,
                   "url_sha256": hashlib.sha256(url.encode()).hexdigest(),
                   "provider": "deployment", "sdk_package": "requests", "sdk_version": __version__,
                   "transport_package": "requests", "transport_version": __version__,
                   **_wire_fingerprints(prepared)}
        if send:
            response = session.send(prepared, timeout=timeout, allow_redirects=False, stream=True)
            receipt["http_status"] = response.status_code
            response.close()
        else:
            receipt.update(
                validation_status="validated_no_send", transport="prepared_request_no_send",
                transport_real_calls=0, network_calls=0, credential_real_calls=0, captured_requests=1,
                authentication_performed=False, provider_send_performed=False,
                provider_response_fabricated=False, fixture_response_counts={"get": 0, "write": 0},
            )
        return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check-generated", action="store_true")
    action.add_argument("--write-generated", action="store_true")
    action.add_argument("--native-cli-no-send", action="store_true")
    action.add_argument("--native-http-no-send", action="store_true")
    action.add_argument("--native-http-send", action="store_true")
    options = parser.parse_args()
    if options.native_cli_no_send or options.native_http_no_send or options.native_http_send:
        import sys
        try:
            packet = json.load(sys.stdin)
            result = (native_cli_no_send(packet) if options.native_cli_no_send
                      else native_http_request(packet, send=options.native_http_send))
        except NativeCLIError as error:
            result = {"status": "rejected", "no_send": True, "reason": "native_client_construction",
                      "diagnostic": error.diagnostic}
        except BaseException:
            result = {"status": "rejected", "no_send": not options.native_http_send,
                      "reason": "native_client_construction_or_transport"}
        print(json.dumps(result, sort_keys=True))
        if result["status"] != "validated":
            raise SystemExit(2)
    elif options.write_generated:
        value = generate_contracts()
        (ASSETS / GENERATED_FILE).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        print("Regenerated four write contracts from verified pinned sources.")
    else:
        check_generated()
        print("Verified four generated write contracts and their official source pins.")


if __name__ == "__main__":
    main()

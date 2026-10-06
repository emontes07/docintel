"""Owner-scoped, append-only reviewer keys and offline, attempt-bound scoring."""

import hashlib
import json
import math
import re
from base64 import b64decode, b64encode
from datetime import datetime, timezone

from backend.batch import BatchService
from backend.batch_store import Conflict, Missing, read_json, write_json
from backend.models.enrichment import AttributeDefinition, EnrichmentResult, Manifest
from backend.workbooks import WorkbookError, read_workbook, read_workbook_cells, write_workbook

SCHEMA_VERSION = 1
TIERS = ("internal_pdf", "vendor_table", "manufacturer_web", "approved_web", "unattributed")
REVIEW_COLUMNS = [
    "Product ID", "MPN", "Attribute", "Decision", "Correction", "Correction unit", "Reason",
]
# Internal normalization only; these columns are never emitted to a customer.
_BOUND_COLUMNS = [
    "Snapshot ID", "Item key", "Item ID", "Vendor", "MPN", "Hierarchy node",
    "Attribute", "Attempt key", "Result SHA256", "Candidate index", "Decision",
    "Corrected value", "Corrected unit", "Reason",
]
_BINDING_COLUMNS = _BOUND_COLUMNS[:9]
PRODUCT_COLUMNS = ("item_id", "vendor", "mpn", "hierarchy_node")
PUBLIC_INPUTS = {"Decision", "Correction", "Correction unit", "Reason"}
PUBLIC_REQUIRED = set(REVIEW_COLUMNS)
ID = re.compile(r"^[a-f0-9]{64}$")


class AnswerKeyError(ValueError):
    """An actionable, data-free workbook/selection validation failure."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def stable_key(product, attribute_id):
    return digest(canonical([*[product[name] for name in PRODUCT_COLUMNS], attribute_id]))


def _binding(snapshot_id, slot):
    return [
        snapshot_id, slot["item_key"], *[slot["product"][name] for name in PRODUCT_COLUMNS],
        slot["attribute_id"], slot["attempt_key"], slot["result_sha256"],
    ]


def _parse_correction(text, unit, definition):
    definition = AttributeDefinition.model_validate(definition)
    try:
        if definition.value_type == "string":
            value = text
        elif definition.value_type == "boolean":
            if text.casefold() not in {"true", "false"}:
                raise ValueError()
            value = text.casefold() == "true"
        elif definition.value_type == "integer":
            if not re.fullmatch(r"[+-]?[0-9]+", text):
                raise ValueError()
            value = int(text)
        else:
            value = float(text)
            if not math.isfinite(value):
                raise ValueError()
        definition.validate_value(value, unit or None)
        return value
    except (ValueError, OverflowError):
        raise AnswerKeyError("Correction must match the attribute type, allowed values, and exact defined unit") from None


def _equal(candidate, judgment):
    # Numeric int/float equivalence is intentional; booleans never equal 0/1.
    left, right = candidate["value"], judgment["value"]
    return (
        (type(left) is bool) == (type(right) is bool)
        and left == right and candidate.get("unit") == judgment.get("unit")
    )


class AnswerKeyService:
    def __init__(self, store):
        self.store = store
        self.batches = BatchService(store)

    @staticmethod
    def _prefix(actor):
        if not isinstance(actor, str) or not actor.strip():
            raise AnswerKeyError("An authenticated owner is required")
        return f"answer-keys/{digest(actor.encode())}/"

    def _get(self, kind, record_id, actor):
        if not ID.fullmatch(record_id):
            raise Missing(record_id)
        record, _ = read_json(self.store, f"{self._prefix(actor)}{kind}/{record_id}.json")
        if record["body"]["owner"] != actor:
            raise Missing(record_id)
        if record.get("id") != record_id or digest(canonical(record["body"])) != record_id:
            raise AnswerKeyError("Immutable scoring record failed its integrity check")
        return record

    def _put(self, kind, body, actor):
        record_id = digest(canonical(body))
        record = {"id": record_id, "created_at": datetime.now(timezone.utc).isoformat(), "body": body}
        try:
            write_json(self.store, f"{self._prefix(actor)}{kind}/{record_id}.json", record)
        except Conflict:
            record = self._get(kind, record_id, actor)
        return record

    def snapshot(self, batch_id, actor):
        """Copy current immutable result attempts; never submit, infer, or review."""
        batch = self.batches.get(batch_id, actor)
        slots = []
        seen = set()
        visible_identities = set()
        pointers = []
        for item in batch["items"]:
            if not item.get("manifest") or item.get("errors"):
                raise AnswerKeyError("All batch items need valid manifests before taking a scoring snapshot")
            manifest = Manifest.model_validate(item["manifest"])
            product = manifest.product.model_dump(mode="json")
            state_key = f"items/{batch_id}/{item['item_key']}.json"
            try:
                state, _ = read_json(self.store, state_key)
            except Missing:
                state = {}
            result_key = self.batches.result_record_key(batch_id, item["item_key"], state)
            try:
                raw, result_version = self.store.read_bytes(result_key)
            except Missing:
                raw, result_version = None, None
            pointers.append((state_key, result_key, result_version))
            machine = EnrichmentResult.model_validate_json(raw) if raw is not None else None
            if machine is not None and machine.manifest != manifest:
                raise AnswerKeyError("Result attempt manifest does not match the batch item")
            attributes = {attribute.attribute_id: attribute for attribute in machine.attributes} if machine else {}
            if machine and (len(attributes) != len(machine.attributes) or set(attributes) != {d.attribute_id for d in manifest.attributes}):
                raise AnswerKeyError("Result must contain each requested attribute exactly once")
            evidence = {entry.evidence_id: entry.model_dump(mode="json") for entry in machine.evidence} if machine else {}
            if machine and len(evidence) != len(machine.evidence):
                raise AnswerKeyError("Result contains duplicate evidence IDs")
            for definition in manifest.attributes:
                key = stable_key(product, definition.attribute_id)
                if key in seen:
                    raise AnswerKeyError("Duplicate stable product + attribute binding in the batch")
                seen.add(key)
                visible_identity = product["item_id"], definition.attribute_id
                if visible_identity in visible_identities:
                    raise AnswerKeyError("Reviewer Product ID + Attribute must identify exactly one slot")
                visible_identities.add(visible_identity)
                attribute = attributes.get(definition.attribute_id)
                candidates = []
                for index, candidate in enumerate(attribute.candidates if attribute else []):
                    if candidate.attribute_id != definition.attribute_id or any(eid not in evidence for eid in candidate.evidence_ids):
                        raise AnswerKeyError("Candidate has an unknown attribute or evidence binding")
                    definition.validate_value(candidate.value, candidate.unit)
                    candidates.append({
                        **candidate.model_dump(mode="json"), "index": index,
                        "tiers": sorted({evidence[eid]["source_tier"] for eid in candidate.evidence_ids}),
                        "evidence": [evidence[eid] for eid in candidate.evidence_ids],
                    })
                slots.append({
                    "key": key, "item_key": item["item_key"], "product": product,
                    "attribute_id": definition.attribute_id, "definition": definition.model_dump(mode="json"),
                    "attempt_key": result_key, "result_sha256": digest(raw) if raw is not None else "unavailable",
                    "status": attribute.status if attribute else "no_result",
                    "existing_value": manifest.existing_values.get(definition.attribute_id),
                    "extraction_error": machine.extraction_error if machine else None,
                    "candidates": candidates,
                })
        # Fail instead of publishing a mixed snapshot when pointers/results advance while reading.
        for state_key, result_key, result_version in pointers:
            try:
                state, _ = read_json(self.store, state_key)
            except Missing:
                state = {}
            item_key = state_key.rsplit("/", 1)[1][:-5]
            if self.batches.result_record_key(batch_id, item_key, state) != result_key:
                raise Conflict("Result pointer advanced during snapshot; retry")
            try:
                _, current_version = self.store.read_bytes(result_key)
            except Missing:
                current_version = None
            if result_version != current_version:
                raise Conflict("Result changed during snapshot; retry")
        if not slots:
            raise AnswerKeyError("A scoring snapshot needs at least one requested attribute")
        snapshot = self._put("snapshots", {
            "schema_version": SCHEMA_VERSION, "owner": actor, "batch_id": batch_id,
            "slots": sorted(slots, key=lambda slot: (
                *[slot["product"][name] for name in PRODUCT_COLUMNS], slot["attribute_id"],
            )),
        }, actor)
        return snapshot

    def get_snapshot(self, snapshot_id, actor):
        return self._get("snapshots", snapshot_id, actor)

    def get_version(self, version_id, actor):
        return self._get("versions", version_id, actor)

    def list_records(self, actor):
        prefix = self._prefix(actor)
        return {
            kind: sorted([
                {"id": record["id"], "created_at": record["created_at"],
                 "batch_id": record["body"].get("batch_id"), "snapshot_id": record["body"].get("snapshot_id"),
                 "previous_version": record["body"].get("previous_version")}
                for key in sorted(self.store.keys(prefix + kind + "/"))
                for record in [self._get(kind, key.rsplit("/", 1)[1][:-5], actor)]
            ], key=lambda record: (record["created_at"], record["id"]), reverse=True)
            for kind in ("snapshots", "versions", "packages")
        }

    def register_baseline(self, snapshot_id, actor, content, candidate_bindings=None):
        """Register an already-issued sanitized layout, never infer its source attempt."""
        snapshot = self.get_snapshot(snapshot_id, actor)
        try:
            sheets = read_workbook(content)
            cells = read_workbook_cells(content)
        except WorkbookError as error:
            raise AnswerKeyError(str(error)) from None
        rows = sheets.get("Review")
        if not rows or any(not PUBLIC_REQUIRED <= set(row) for row in rows):
            raise AnswerKeyError("Baseline Review needs Product ID, MPN, Attribute, Decision, Correction, Correction unit, Reason")
        for sheet_rows in cells.values():
            if sheet_rows and any(re.search(r"(snapshot|attempt|session|receipt|hash|sha256|itemkey|candidateindex)",
                                           re.sub(r"[^a-z0-9]", "", cell.text.casefold()))
                                  for cell in sheet_rows[0].cells.values()):
                raise AnswerKeyError("Customer baseline must not contain technical binding columns, including hidden columns")
            if any(re.search(r"(?i)(?:\b[a-f0-9]{64}\b|\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b|(?:batchblob|file|az|blob)://|/(?:Users|home|tmp|var)/|results/[^ ]+|https?://[^ ]+\.(?:blob|dfs)\.core\.windows\.net/)", cell.text)
                   for row in sheet_rows for cell in row.cells.values()):
                raise AnswerKeyError("Customer baseline contains private hashes, identifiers, or internal paths")
        slots = {(slot["product"]["item_id"], slot["attribute_id"]): slot for slot in snapshot["body"]["slots"]}
        selections = {}
        if candidate_bindings is not None:
            if not isinstance(candidate_bindings, list):
                raise AnswerKeyError("Private candidate bindings must be a list")
            for binding in candidate_bindings:
                if not isinstance(binding, dict) or set(binding) != {"product_id", "attribute_id", "candidate_index"}:
                    raise AnswerKeyError("Each private binding needs product_id, attribute_id, candidate_index")
                if not isinstance(binding["product_id"], str) or not isinstance(binding["attribute_id"], str):
                    raise AnswerKeyError("Private binding identities must be exact strings")
                identity = binding["product_id"], binding["attribute_id"]
                index = binding["candidate_index"]
                if identity not in slots or identity in selections:
                    raise AnswerKeyError("Unknown or duplicate private candidate binding")
                if index is not None and (type(index) is not int or not 0 <= index < len(slots[identity]["candidates"])):
                    raise AnswerKeyError("Private candidate binding references an unavailable candidate")
                selections[identity] = index
        bindings = {}
        for row_number, row in enumerate(rows, 2):
            identity = row["Product ID"], row["Attribute"]
            if identity not in slots or identity in bindings:
                raise AnswerKeyError(f"Baseline row {row_number}: unknown or duplicate Product ID + Attribute")
            slot = slots[identity]
            if row["MPN"] != slot["product"]["mpn"]:
                raise AnswerKeyError(f"Baseline row {row_number}: MPN differs from the immutable snapshot")
            if any(row[column].strip() for column in PUBLIC_INPUTS):
                raise AnswerKeyError(f"Baseline row {row_number}: trusted baseline decision fields must be blank")
            index = selections.get(identity, 0 if len(slot["candidates"]) == 1 else None)
            bindings[identity] = {
                "slot_key": slot["key"], "item_key": slot["item_key"], "product": slot["product"],
                "attribute_id": slot["attribute_id"], "attempt_key": slot["attempt_key"],
                "result_sha256": slot["result_sha256"], "candidate_index": index,
                "readonly_row_sha256": digest(canonical({name: value for name, value in row.items() if name not in PUBLIC_INPUTS})),
            }
        if set(selections) - set(bindings):
            raise AnswerKeyError("Private candidate binding references a row outside the registered baseline")
        readonly = {
            name: [{key: value for key, value in row.items() if name != "Review" or key not in PUBLIC_INPUTS} for row in sheet_rows]
            for name, sheet_rows in sheets.items()
        }
        # Rebuild data-only cells to exclude hidden workbook/package metadata.
        normalized = write_workbook({
            name: [
                [cell.text for cell in cell_rows[0].cells.values()],
                *[[row.get(cell.text, "") for cell in cell_rows[0].cells.values()] for row in sheets[name]],
            ]
            for name, cell_rows in cells.items() if cell_rows
        })
        return self._put("packages", {
            "schema_version": SCHEMA_VERSION, "owner": actor, "snapshot_id": snapshot_id,
            "baseline_sha256": digest(content), "readonly_content_sha256": digest(canonical(readonly)),
            "workbook_base64": b64encode(normalized).decode("ascii"),
            "bindings": {binding["slot_key"]: binding for binding in bindings.values()},
        }, actor)

    def _package(self, snapshot_id, actor, package_id=None):
        if package_id is not None:
            package = self._get("packages", package_id, actor)
            if package["body"]["snapshot_id"] != snapshot_id:
                raise AnswerKeyError("Reviewer package belongs to another immutable snapshot")
            return package
        packages = [
            package for key in self.store.keys(self._prefix(actor) + "packages/")
            for package in [self._get("packages", key.rsplit("/", 1)[1][:-5], actor)]
            if package["body"]["snapshot_id"] == snapshot_id
        ]
        if len(packages) != 1:
            raise AnswerKeyError("Register a trusted reviewer baseline first, and select its package_id when multiple baselines exist")
        return packages[0]

    def template(self, snapshot_id, actor, package_id=None):
        self.get_snapshot(snapshot_id, actor)
        return b64decode(self._package(snapshot_id, actor, package_id)["body"]["workbook_base64"], validate=True)

    def ingest(self, snapshot_id, actor, content, previous_version=None, package_id=None):
        snapshot = self.get_snapshot(snapshot_id, actor)
        try:
            sheets = read_workbook(content)
            cells = read_workbook_cells(content).get("Review", [])
        except WorkbookError as error:
            raise AnswerKeyError(str(error)) from None
        rows = sheets.get("Review")
        if not rows:
            raise AnswerKeyError("Review sheet must contain the exported columns and data rows")
        package = self._package(snapshot_id, actor, package_id)
        expected = read_workbook(b64decode(package["body"]["workbook_base64"], validate=True))
        if set(sheets) != set(expected):
            raise AnswerKeyError("Retain all worksheets from the issued reviewer package; unknown worksheets are not allowed")
        for name in set(sheets) - {"Review"}:
            if sheets[name] != expected[name]:
                raise AnswerKeyError("Read-only reviewer context changed; only edit decision fields on Review")
        slots = {(slot["product"]["item_id"], slot["attribute_id"]): slot for slot in snapshot["body"]["slots"]}
        expected_identities = {
            (binding["product"]["item_id"], binding["attribute_id"])
            for binding in package["body"]["bindings"].values()
        }
        seen = set()
        bound_rows = [_BOUND_COLUMNS]
        for row_number, row in enumerate(rows, 2):
            if set(row) != set(expected["Review"][0]):
                raise AnswerKeyError(f"Review row {row_number}: headers must match the issued reviewer package")
            identity = row["Product ID"], row["Attribute"]
            if identity not in expected_identities or identity in seen:
                raise AnswerKeyError(f"Review row {row_number}: unknown or duplicate Product ID + Attribute")
            seen.add(identity)
            private_binding = package["body"]["bindings"][slots[identity]["key"]]
            readonly = {name: value for name, value in row.items() if name not in PUBLIC_INPUTS}
            if digest(canonical(readonly)) != private_binding["readonly_row_sha256"]:
                raise AnswerKeyError(f"Review row {row_number}: product or read-only proposal/evidence context changed")
            slot = slots[identity]
            for column, cell in cells[row_number - 1].cells.items():
                if cell.kind == "b":
                    title = cells[0].cells[column].text
                    if title != "Correction" or slot["definition"]["value_type"] != "boolean" or cell.value not in {"0", "1"}:
                        raise AnswerKeyError(f"Review row {row_number}: native Boolean cells are only valid as Boolean corrections")
                    row[title] = "true" if cell.value == "1" else "false"
            candidate_index = private_binding["candidate_index"] if row["Decision"].strip() in {"Approve", "Reject"} else None
            if row["Decision"].strip() == "Approve" and candidate_index is None:
                raise AnswerKeyError(f"Review row {row_number}: Approve needs an unambiguous privately bound proposal; use Correct for a reference answer")
            bound_rows.append([
                *_binding(snapshot_id, slot), "" if candidate_index is None else str(candidate_index), row["Decision"],
                row["Correction"], row["Correction unit"], row["Reason"],
            ])
        if seen != expected_identities:
            raise AnswerKeyError("Review sheet is missing registered Product ID + Attribute rows; retain blank decisions for unreviewed rows")
        for identity in slots.keys() - expected_identities:
            bound_rows.append([*_binding(snapshot_id, slots[identity]), "", "", "", "", ""])
        return self._ingest_bound(
            snapshot_id, actor, write_workbook({"Review": bound_rows}), previous_version,
            workbook_sha256=digest(content), package_id=package["id"],
        )

    def _ingest_bound(self, snapshot_id, actor, content, previous_version=None, *, workbook_sha256=None, package_id=None):
        snapshot = self.get_snapshot(snapshot_id, actor)
        if previous_version is not None:
            previous = self.get_version(previous_version, actor)
            if previous["body"]["snapshot_id"] != snapshot_id:
                raise AnswerKeyError("Previous answer-key version belongs to another snapshot")
        try:
            sheets = read_workbook(content)
            cells = read_workbook_cells(content).get("Review", [])
        except WorkbookError as error:
            raise AnswerKeyError(str(error)) from None
        unknown_sheets = set(sheets) - {"Review", "Read me", "Candidates", "Evidence", "Definitions"}
        if unknown_sheets:
            raise AnswerKeyError("Unknown worksheet; use the exported scoring template")
        rows = sheets.get("Review")
        if not rows:
            raise AnswerKeyError("Review sheet must contain the exported binding columns and data rows")
        slots = {(slot["item_key"], slot["attribute_id"]): slot for slot in snapshot["body"]["slots"]}
        seen = set()
        judgments = {}
        for row_number, row in enumerate(rows, 2):
            try:
                if set(row) != set(_BOUND_COLUMNS):
                    raise AnswerKeyError("Review headers must exactly match the exported template")
                binding = row["Item key"], row["Attribute"]
                if binding not in slots:
                    raise AnswerKeyError("Unknown item or attribute binding")
                if binding in seen:
                    raise AnswerKeyError("Duplicate item + attribute review")
                seen.add(binding)
                slot = slots[binding]
                if [row[column] for column in _BINDING_COLUMNS] != _binding(snapshot_id, slot):
                    raise AnswerKeyError("Product, snapshot, or source-attempt binding differs from the immutable snapshot")
                for column, cell in cells[row_number - 1].cells.items():
                    if cell.kind == "b":
                        title = cells[0].cells[column].text
                        if title != "Corrected value" or slot["definition"]["value_type"] != "boolean" or cell.value not in {"0", "1"}:
                            raise AnswerKeyError("Native Boolean cells are only valid as Boolean Corrected values")
                        row[title] = "true" if cell.value == "1" else "false"
                decision = row["Decision"].strip()
                index_text = row["Candidate index"].strip()
                correction = row["Corrected value"]
                unit = row["Corrected unit"]
                reason = row["Reason"].strip()
                if not decision:
                    if any(value.strip() for value in (index_text, correction, unit, reason)):
                        raise AnswerKeyError("A blank Decision cannot carry a candidate index, correction, unit, or reason")
                    continue
                if decision not in {"Approve", "Correct", "Reject"}:
                    raise AnswerKeyError("Decision must be Approve, Correct, Reject, or blank")
                if not reason or len(reason) > 2000:
                    raise AnswerKeyError("Every decision requires a Reason of 1–2000 characters")
                judgment = {"decision": decision, "reason": reason, "candidate_index": None}
                if decision == "Approve":
                    if not re.fullmatch(r"0|[1-9][0-9]*", index_text) or int(index_text) >= len(slot["candidates"]):
                        raise AnswerKeyError("Approve requires an existing zero-based Candidate index")
                    if correction.strip() or unit.strip():
                        raise AnswerKeyError("Approve cannot contain corrections; use Correct instead")
                    index = int(index_text)
                    candidate = slot["candidates"][index]
                    judgment.update(candidate_index=index, value=candidate["value"], unit=candidate.get("unit"))
                elif decision == "Correct":
                    if index_text:
                        raise AnswerKeyError("Correct supplies the final answer; leave Candidate index blank")
                    if not correction.strip():
                        raise AnswerKeyError("Correct requires a nonblank Corrected value")
                    judgment.update(value=_parse_correction(correction, unit, slot["definition"]), unit=unit or None)
                else:
                    if correction.strip() or unit.strip():
                        raise AnswerKeyError("Reject applies only to this reviewer row; leave corrections blank")
                    if index_text:
                        if not re.fullmatch(r"0|[1-9][0-9]*", index_text) or int(index_text) >= len(slot["candidates"]):
                            raise AnswerKeyError("Reject references an unavailable privately bound candidate")
                        judgment["candidate_index"] = int(index_text)
                judgments[slot["key"]] = judgment
            except AnswerKeyError as error:
                raise AnswerKeyError(f"Review row {row_number}: {error}") from None
        if seen != set(slots):
            raise AnswerKeyError("Review sheet is missing exported item + attribute rows; retain blank decisions for unreviewed rows")
        if not judgments:
            raise AnswerKeyError("No explicit reviewer decisions found; machine candidates are never auto-approved")
        return self._put("versions", {
            "schema_version": SCHEMA_VERSION, "owner": actor, "snapshot_id": snapshot_id,
            "previous_version": previous_version, "workbook_sha256": workbook_sha256 or digest(content),
            "package_id": package_id, "judgments": judgments,
        }, actor)

    def _judgments(self, snapshot, version_id, actor):
        if version_id is None:
            return {}
        version = self.get_version(version_id, actor)
        source = self.get_snapshot(version["body"]["snapshot_id"], actor)
        source_slots = {slot["key"]: slot for slot in source["body"]["slots"]}
        judgments = {}
        for slot in snapshot["body"]["slots"]:
            judgment = version["body"]["judgments"].get(slot["key"])
            original = source_slots.get(slot["key"])
            if not judgment or not original or original["definition"] != slot["definition"]:
                continue
            same_attempt = (
                original["attempt_key"], original["result_sha256"]
            ) == (slot["attempt_key"], slot["result_sha256"])
            if judgment["decision"] == "Reject" and not same_attempt:
                continue  # A rejection is not a transferable ground-truth value.
            judgments[slot["key"]] = {
                **judgment, "review_scope": "direct" if same_attempt else "transferred_gold",
            }
        return judgments

    def score(self, snapshot_id, actor, version_id=None):
        snapshot = self.get_snapshot(snapshot_id, actor)
        slots = snapshot["body"]["slots"]
        judgments = self._judgments(snapshot, version_id, actor)
        return {
            "schema_version": SCHEMA_VERSION, "snapshot_id": snapshot_id, "answer_key_id": version_id,
            "qualification": "Accuracy measures value agreement with explicit human judgments, segmented by evidence basis. Reviewed inference is not literal evidence, certification, or automatic approval. Null means no judged denominator.",
            "overall": _metrics(slots, judgments),
            "by_attribute": {
                name: _metrics([slot for slot in slots if slot["attribute_id"] == name], judgments)
                for name in sorted({slot["attribute_id"] for slot in slots})
            },
            "by_tier": {tier: _metrics(slots, judgments, tier=tier) for tier in TIERS},
            "by_evidence_basis": {
                basis: _metrics(slots, judgments, basis=basis)
                for basis in ("literal", "inferred_from_description")
            },
        }

    def delta(self, snapshot_id, previous_id, actor, version_id=None):
        current = self.get_snapshot(snapshot_id, actor)
        previous = self.get_snapshot(previous_id, actor)
        before = {slot["key"]: slot for slot in previous["body"]["slots"]}
        after = {slot["key"]: slot for slot in current["body"]["slots"]}
        rows = []
        for key in sorted(before.keys() | after.keys()):
            old, new = before.get(key), after.get(key)
            change = "added" if old is None else "removed" if new is None else "unchanged"
            fields = []
            if old is not None and new is not None:
                fields = [
                    name for name in ("definition", "status", "candidates", "attempt_key", "result_sha256")
                    if old[name] != new[name]
                ]
                if fields:
                    change = "changed"
            slot = new or old
            rows.append({
                "key": key, "product": slot["product"], "attribute_id": slot["attribute_id"],
                "change": change, "changed_fields": fields, "before": old, "after": new,
            })
        return {
            "snapshot_id": snapshot_id, "previous_snapshot_id": previous_id, "answer_key_id": version_id,
            "counts": {name: sum(row["change"] == name for row in rows) for name in ("added", "removed", "changed", "unchanged")},
            "rows": rows, "before_score": self.score(previous_id, actor, version_id),
            "after_score": self.score(snapshot_id, actor, version_id),
        }


def _metrics(slots, judgments, *, tier=None, basis=None):
    result = dict.fromkeys((
        "eligible_slots", "reviewed_slots", "unreviewed_slots", "gold_slots", "rejected_slots",
        "output_slots", "abstentions", "conflicts", "reviewed_conflicts", "single_outputs",
        "scorable_single_outputs", "correct_single_outputs", "candidate_count",
        "judged_candidates", "correct_candidates", "literal_candidates", "inferred_candidates",
        "existing_slots", "no_result_slots", "directly_reviewed_slots", "transferred_gold_slots",
        "reviewed_inferred_candidates", "inferred_value_agreements", "unjudged_candidates",
    ), 0)
    for slot in slots:
        candidates = [
            candidate for candidate in slot["candidates"]
            if (tier is None or tier in (candidate["tiers"] or ["unattributed"]))
            and (basis is None or candidate["evidence_basis"] == basis)
        ]
        judgment = judgments.get(slot["key"])
        gold = judgment is not None and judgment["decision"] != "Reject"
        # A slice must not turn a conflict into a single accepted output.
        conflict = bool(candidates) and (slot["status"] == "conflict" or len(slot["candidates"]) > 1)
        single = bool(candidates) and not conflict
        literal = [candidate for candidate in candidates if candidate["evidence_basis"] == "literal"]
        inferred = [candidate for candidate in candidates if candidate["evidence_basis"] != "literal"]
        judged = [
            candidate for candidate in candidates
            if judgment and (gold or candidate["index"] == judgment["candidate_index"])
        ]
        judged_inferred = [candidate for candidate in judged if candidate["evidence_basis"] != "literal"]
        correct = sum(_equal(candidate, judgment) for candidate in judged) if gold else 0
        result["eligible_slots"] += 1
        result["reviewed_slots" if judgment else "unreviewed_slots"] += 1
        result["directly_reviewed_slots"] += judgment is not None and judgment["review_scope"] == "direct"
        result["transferred_gold_slots"] += judgment is not None and judgment["review_scope"] == "transferred_gold"
        result["gold_slots"] += gold
        result["rejected_slots"] += judgment is not None and not gold
        result["output_slots"] += bool(candidates)
        result["abstentions"] += not candidates
        result["conflicts"] += conflict
        result["reviewed_conflicts"] += conflict and judgment is not None
        result["single_outputs"] += single
        result["scorable_single_outputs"] += single and bool(judged)
        result["correct_single_outputs"] += single and bool(correct)
        result["candidate_count"] += len(candidates)
        result["judged_candidates"] += len(judged)
        result["correct_candidates"] += correct
        result["literal_candidates"] += len(literal)
        result["inferred_candidates"] += sum(c["evidence_basis"] == "inferred_from_description" for c in candidates)
        result["reviewed_inferred_candidates"] += len(judged_inferred)
        result["inferred_value_agreements"] += sum(_equal(candidate, judgment) for candidate in inferred) if gold else 0
        result["unjudged_candidates"] += len(candidates) - len(judged)
        result["existing_slots"] += slot["status"] == "existing"
        result["no_result_slots"] += slot["status"] == "no_result"
    for name, numerator, denominator in (
        ("accuracy", "correct_single_outputs", "scorable_single_outputs"),
        ("candidate_accuracy", "correct_candidates", "judged_candidates"),
        ("review_coverage", "reviewed_slots", "eligible_slots"),
        ("gold_coverage", "gold_slots", "eligible_slots"),
        ("output_coverage", "output_slots", "eligible_slots"),
        ("correct_coverage", "correct_single_outputs", "gold_slots"),
    ):
        result[name] = result[numerator] / result[denominator] if result[denominator] else None
    return result

import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import * as React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";

const require = createRequire(import.meta.url);

function loadFrontend(path, overrides = {}) {
  const cache = new Map();
  function load(url, replacements = {}) {
    if (cache.has(url.href)) return cache.get(url.href);
    const compiledModule = { exports: {} };
    cache.set(url.href, compiledModule.exports);
    const source = readFileSync(url, "utf8");
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
    const localRequire = name => {
      if (Object.hasOwn(replacements, name)) return replacements[name];
      if (name.startsWith("@/")) {
        const dependency = [".tsx", ".ts"].map(extension => new URL(`../${name.slice(2)}${extension}`, import.meta.url)).find(existsSync);
        assert.ok(dependency, `Unknown frontend import: ${name}`);
        return load(dependency);
      }
      return require(name);
    };
    new Function("require", "module", "exports", compiled)(localRequire, compiledModule, compiledModule.exports);
    return compiledModule.exports;
  }
  return load(new URL(path, import.meta.url), overrides);
}

const { ValidationDiagnostics } = loadFrontend("../components/validation-diagnostics.tsx");
const { EnrichmentReview } = loadFrontend("../components/enrichment-review.tsx");
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));
const escaped = value => value.replace(/[&<>"']/g, character => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#x27;" })[character]);

function diagnostic(overrides = {}) {
  return {
    schema_version: 1,
    stage: "evidence_validation",
    issues: [{ field_path: "attributes[0].evidence_ids[0]", message: "Reference was not supplied." }],
    used_references: [{ field_path: "attributes[0].evidence_ids[0]", value: "unsupported-reference", sha256: "a".repeat(64), redacted: false }],
    valid_references: ["owner-source#page=1"],
    parsed_response: { attributes: [{ attribute_id: "Pressure Rating", value: 200, unit: "psi" }], reviewed: false, note: null },
    raw_response_sha256: "b".repeat(64),
    raw_response_hash_basis: "provider_content",
    source_tier: "manufacturer",
    reservation_id: "offline-reservation",
    prompt_format: "structured-json-v1",
    truncated: false,
    ...overrides,
  };
}

function machine(attribute = {}) {
  return {
    attributes: [{ attribute_id: "Pressure Rating", status: "missing", candidates: [], review: null, review_annotations: [], ...attribute }],
    evidence: [],
    manifest: { attributes: [{ attribute_id: "Pressure Rating", value_type: "number", unit: "psi" }], existing_values: {} },
  };
}

function detail(overrides = {}) {
  return {
    state: "failed",
    requested_mode: "offline_replay",
    original: { "PIMITEM Number": "OWNER-1", "Vendor Name": "Offline fixture", MPN: "PART-1" },
    row: 2,
    error: "Evidence validation failed: unsupported reference.",
    provenance: [{ reference: "owner-source", retrieval: "succeeded", parsing: "failed", error: "Source parsing failed." }],
    machine_sha256: "c".repeat(64),
    reviewed_result: null,
    reviewer_identity: "verified_entra",
    ...overrides,
  };
}

function renderDetail(value) {
  const { default: BatchItemPage } = loadFrontend("../app/batches/[batchId]/[itemKey]/page.tsx", {
    react: {
      ...React,
      use: () => ({ batchId: "offline-owner-batch", itemKey: "row-2" }),
      useState: initial => React.useState(initial === null ? value : initial),
    },
  });
  return render(BatchItemPage, { params: Promise.resolve({ batchId: "offline-owner-batch", itemKey: "row-2" }) });
}

test("diagnostics render recorded issues, references, parsed structure, hashes and metadata", () => {
  const value = diagnostic({ truncated: true });
  const html = render(ValidationDiagnostics, { diagnostics: [value] });
  for (const text of ["Validation diagnostics", "Evidence validation", "Issues and field paths", value.issues[0].field_path, value.issues[0].message, "Supplied references", "unsupported-reference", "Reference SHA-256", value.used_references[0].sha256, "Not redacted", "Valid references", "owner-source#page=1", "Parsed response (sanitized/redacted)", "Raw response SHA-256", value.raw_response_sha256, "Provider content", "Source tier", value.source_tier, "Reservation ID", value.reservation_id, "Prompt format", value.prompt_format, "Diagnostic schema version"]) {
    assert.ok(html.includes(text), `Missing recorded diagnostic text: ${text}`);
  }
  assert.ok(html.includes(escaped(JSON.stringify(value.parsed_response, null, 2))));
  assert.match(html, /<pre[^>]*>/);
  assert.match(html, /Diagnostic output was truncated/);
});

test("every diagnostic field is text and redacted supplied values remain hidden", () => {
  const attack = '<img src=x onerror="alert(1)"> & <script>alert(2)</script>';
  const value = diagnostic({
    issues: [{ field_path: `path-${attack}`, message: `issue-${attack}` }],
    used_references: [
      { field_path: `reference-${attack}`, value: `value-${attack}`, sha256: `hash-${attack}`, redacted: false },
      { field_path: "private-source", value: "DO-NOT-DISPLAY-REDACTED-VALUE", sha256: "d".repeat(64), redacted: true },
      { field_path: "missing-source", value: null, sha256: "e".repeat(64), redacted: false },
    ],
    valid_references: [`<a href="javascript:alert(3)">${attack}</a>`],
    parsed_response: { [attack]: [{ nested: `<iframe srcdoc="${attack}"></iframe>` }] },
    raw_response_sha256: `raw-${attack}`,
    source_tier: `tier-${attack}`,
    reservation_id: `reservation-${attack}`,
    prompt_format: `format-${attack}`,
  });
  const html = render(ValidationDiagnostics, { diagnostics: [value] });
  for (const prefix of ["path", "issue", "reference", "value", "hash", "raw", "tier", "reservation", "format"]) {
    assert.ok(html.includes(escaped(`${prefix}-${attack}`)), `Field was not rendered as text: ${prefix}`);
  }
  assert.ok(html.includes(escaped(value.valid_references[0])));
  assert.ok(html.includes(escaped(JSON.stringify(value.parsed_response, null, 2))));
  assert.doesNotMatch(html, /<(?:script|img|iframe|svg|a)[\s>]/i);
  assert.doesNotMatch(html, /DO-NOT-DISPLAY-REDACTED-VALUE/);
  assert.match(html, /Value: Redacted/);
  assert.match(html, /Value: Unavailable/);
  assert.ok(html.includes("d".repeat(64)));
});

test("legacy absent or empty diagnostics do not imply validation success", () => {
  for (const diagnostics of [undefined, []]) {
    const html = render(ValidationDiagnostics, { diagnostics });
    assert.match(html, /Validation diagnostics/);
    assert.match(html, /No validation diagnostics recorded\. This does not establish successful validation\./);
    assert.doesNotMatch(html, /<article/);
  }
});

test("parsing diagnostics qualify unavailable raw hashes and missing metadata", () => {
  const html = render(ValidationDiagnostics, { diagnostics: [diagnostic({ stage: "structured_response_parsing", issues: [], used_references: [], valid_references: [], parsed_response: {}, raw_response_sha256: null, raw_response_hash_basis: "unavailable", source_tier: null, reservation_id: null, prompt_format: null })] });
  assert.match(html, /Structured response parsing/);
  assert.match(html, /Unavailable — no raw provider-content hash recorded\./);
  assert.match(html, /Raw response hash basis<\/dt><dd>Unavailable/);
  assert.match(html, /No issues recorded in this diagnostic\./);
  assert.match(html, /No supplied references recorded\./);
  assert.match(html, /No valid references recorded\./);
  assert.equal((html.match(/Not recorded/g) || []).length, 3);
  assert.doesNotMatch(html, /Diagnostic output was truncated/);
});

test("owner detail shows sidecar diagnostics without a machine result and preserves failures", () => {
  const html = renderDetail(detail({ validation_diagnostics: [diagnostic()], machine_result: null }));
  assert.match(html, /Product evidence and review/);
  assert.match(html, /Evidence validation failed: unsupported reference\./);
  assert.match(html, /Source parsing failed\./);
  assert.match(html, /Reference was not supplied\./);
  assert.doesNotMatch(html, /Record decision/);
});

test("owner detail uses the merged top-level diagnostics without duplicating machine diagnostics", () => {
  const recorded = diagnostic();
  const html = renderDetail(detail({ validation_diagnostics: [recorded], machine_result: { validation_diagnostics: [recorded] }, reviewed_result: { ...machine(), validation_diagnostics: [recorded] } }));
  assert.equal((html.match(/Reference was not supplied\./g) || []).length, 1);
  const empty = renderDetail(detail({ validation_diagnostics: [], machine_result: { validation_diagnostics: [recorded] } }));
  assert.match(empty, /No validation diagnostics recorded/);
  assert.doesNotMatch(empty, /Reference was not supplied/);
});

test("owner detail supports machine-only diagnostics and legacy records with none", () => {
  for (const fallback of [{ machine_result: { validation_diagnostics: [diagnostic()] } }, { reviewed_result: { ...machine(), validation_diagnostics: [diagnostic()] } }]) {
    assert.match(renderDetail(detail(fallback)), /Reference was not supplied\./);
  }
  const html = renderDetail(detail({ reviewed_result: machine() }));
  assert.match(html, /No validation diagnostics recorded/);
  assert.match(html, /No supported candidate proposed\./);
  assert.match(html, /Evidence validation failed: unsupported reference\./);
  assert.doesNotMatch(html, /Customer input clarification needed/);
});

test("definition clarification is customer input, renders safely, and disables absent-candidate approval", () => {
  const clarification = 'Specify pressure at <img src=x onerror="alert(1)"> temperature.';
  const html = render(EnrichmentReview, { result: machine({ status: "definition_clarification_needed", definition_clarification: clarification }), identity: "verified_entra", onReview: async () => {} });
  assert.match(html, /Customer input needed/);
  assert.match(html, /Customer input clarification needed/);
  assert.match(html, /This status requests customer input; it is not an extraction failure\./);
  assert.ok(html.includes(escaped(clarification)));
  assert.doesNotMatch(html, /<img/);
  assert.match(html, /No candidate is available to approve while the definition needs clarification\./);
  assert.match(html, /<option value="approve" disabled="">Approve candidate<\/option>/);
  assert.match(html, /<option value="correct">Correct<\/option>/);
  assert.match(html, /<option value="reject">Reject<\/option>/);
  assert.doesNotMatch(html, /No supported candidate proposed\./);
});

test("clarification without recorded wording stays explicit instead of inventing an explanation", () => {
  for (const definition_clarification of [undefined, null]) {
    const html = render(EnrichmentReview, { result: machine({ status: "definition_clarification_needed", definition_clarification }), identity: "verified_entra" });
    assert.match(html, /Clarify the attribute definition before a supported value can be proposed\./);
    assert.match(html, /Customer input clarification needed/);
  }
});

function findElement(node, predicate) {
  if (!React.isValidElement(node)) return null;
  if (predicate(node)) return node;
  for (const child of React.Children.toArray(node.props.children)) {
    const found = findElement(child, predicate);
    if (found) return found;
  }
  return null;
}

async function submitApproval(candidates, selectedIndex) {
  const values = ["approve", "", "Recorded test reason", "", selectedIndex, false, ""];
  const updates = [];
  let stateIndex = 0;
  const { EnrichmentReview: Review } = loadFrontend("../components/enrichment-review.tsx", {
    react: {
      ...React,
      useState: () => {
        const index = stateIndex++;
        return [values[index], value => updates.push({ index, value })];
      },
      useRef: value => ({ current: value }),
    },
  });
  const requests = [];
  const tree = Review({ result: machine({ candidates }), identity: "verified_entra", onReview: async value => { requests.push(value); } });
  const decisionForm = findElement(tree, element => typeof element.type === "function" && element.type.name === "DecisionForm");
  assert.ok(decisionForm);
  const form = decisionForm.type(decisionForm.props);
  await form.props.onSubmit({ preventDefault() {} });
  return { requests, updates };
}

test("stale approval selections cannot submit nonexistent candidates", async () => {
  const candidate = { value: 200, unit: "psi", evidence_ids: [] };
  for (const [candidates, selectedIndex] of [[[], "0"], [[candidate], "9"]]) {
    const { requests, updates } = await submitApproval(candidates, selectedIndex);
    assert.equal(requests.length, 0);
    assert.ok(updates.some(update => update.value === "Select an available candidate before approving."));
  }
});

test("supported candidates retain the existing approval request contract", async () => {
  const { requests } = await submitApproval([{ value: 200, unit: "psi", evidence_ids: [] }], "0");
  assert.deepEqual(requests, [{ attribute_id: "Pressure Rating", decision: "approve", reason: "Recorded test reason", candidate_index: 0, corrected_value: null, corrected_unit: null }]);
});

import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

export function verifyExportWorkbook(evidence, python) {
  if (!python) throw new Error("Explicit approved Python executable required for workbook verification");
  const code = "import base64,json,sys; from scripts.release_fixture import verify_export; data=json.load(sys.stdin); verify_export(base64.b64decode(data['exportBase64'], validate=True), data['batchId'], data['verifiedOwner'], data['machineHashes'])";
  const result = spawnSync(python, ["-c", code], {
    cwd: fileURLToPath(new URL("../../", import.meta.url)),
    input: JSON.stringify(evidence),
    encoding: "utf8",
    env: { ...process.env, PYTHONDONTWRITEBYTECODE: "1" },
    timeout: 30000,
  });
  if (result.error || result.status !== 0) throw new Error("Export workbook-cell verification failed; do not repeat hosted writes");
  return true;
}

export async function intake(page, { origin, manifestBase64, attributesBase64, expectedOwner, approved }) {
  if (approved !== "hosted-synthetic-only" || !origin.startsWith("https://")) throw new Error("Approved hosted synthetic acceptance required");
  await page.goto(`${origin}/batches`);
  const outcome = await page.evaluate(async ({ manifestBase64, attributesBase64, expectedOwner }) => {
    const require = (condition, message) => { if (!condition) throw new Error(message); };
    const unauthenticated = await fetch("/api/batches", { credentials: "omit", cache: "no-store" });
    require(unauthenticated.status === 401, "Unauthenticated frontend API did not reject");
    const catalogResponse = await fetch("/api/batches/catalog", { cache: "no-store" });
    require(catalogResponse.status === 200, "Hosted sign-in/catalog failed");
    const catalog = await catalogResponse.json();
    require(catalog.identity === expectedOwner, "Verified tenant/object identity mismatch");
    const data = new FormData();
    data.append("manifest", new Blob([Uint8Array.from(atob(manifestBase64), character => character.charCodeAt(0))]), "manifest.xlsx");
    data.append("attributes", new Blob([Uint8Array.from(atob(attributesBase64), character => character.charCodeAt(0))]), "definitions.xlsx");
    data.append("attribute_reference", "definitions.xlsx");
    const validated = await fetch("/api/batches/validate", { method: "POST", body: data });
    require(validated.status === 200, "Synthetic XLSX validation failed");
    const batch = await validated.json();
    require(batch.valid && batch.product_count === 2, "Wrong fixture/validation result");
    const request = { request_id: crypto.randomUUID(), mode: "evidence_only", confirm_live: false };
    const submit = () => fetch(`/api/batches/${batch.id}/submit`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(request) });
    const first = await submit();
    require(first.status === 200, "Submission failed");
    const repeated = await submit();
    require(repeated.status === 200 && (await repeated.json()).id === batch.id, "Duplicate submission failed");
    return { batchId: batch.id, requestId: request.request_id, owner: expectedOwner, mode: "evidence_only" };
  }, { manifestBase64, attributesBase64, expectedOwner });
  return outcome;
}

export async function observeSlice(page, { origin, batchId, finished, firstMachineHash, approved }) {
  if (approved !== "hosted-synthetic-only" || !origin.startsWith("https://") || ![1, 2].includes(finished)) throw new Error("Approved bounded synthetic observation required");
  await page.goto(`${origin}/batches/${batchId}`);
  return page.evaluate(async ({ batchId, finished, firstMachineHash }) => {
    const response = await fetch(`/api/batches/${batchId}`, { cache: "no-store" });
    if (response.status !== 200) throw new Error("Cannot read authenticated batch");
    const summary = await response.json();
    if (summary.progress.finished !== finished || summary.state !== (finished === 1 ? "queued" : "completed")) throw new Error("Slice not in expected durable state; inspect execution, do not resubmit");
    const first = await fetch(`/api/batches/${batchId}/items/row-2`, { cache: "no-store" });
    if (first.status !== 200) throw new Error("First product unavailable");
    const detail = await first.json();
    if (detail.machine_result.model_call_status !== "not_attempted" || (firstMachineHash && detail.machine_sha256 !== firstMachineHash)) throw new Error("First product changed or invoked a model");
    return { batchId, finished, firstMachineHash: detail.machine_sha256 };
  }, { batchId, finished, firstMachineHash });
}

export async function verifyOtherOwner(page, { origin, batchId, expectedOwner, approved }) {
  if (approved !== "hosted-synthetic-only" || !origin.startsWith("https://")) throw new Error("Approved hosted synthetic acceptance required");
  await page.goto(`${origin}/batches`);
  return page.evaluate(async ({ batchId, expectedOwner }) => {
    const catalog = await fetch("/api/batches/catalog", { cache: "no-store" });
    if (catalog.status !== 200 || (await catalog.json()).identity !== expectedOwner) throw new Error("Second authenticated owner mismatch");
    for (const suffix of ["", "/items/row-2", "/items/row-3", "/export"]) {
      if ((await fetch(`/api/batches/${batchId}${suffix}`, { cache: "no-store" })).status !== 404) throw new Error("Cross-owner read was not rejected");
    }
    return { ownerIsolation: true };
  }, { batchId, expectedOwner });
}

export async function reviewAndExport(page, { origin, batchId, expectedOwner, approved, python }) {
  if (approved !== "hosted-synthetic-only" || !origin.startsWith("https://")) throw new Error("Approved hosted synthetic acceptance required");
  if (!python) throw new Error("Explicit approved Python executable required before hosted review");
  await page.goto(`${origin}/batches/${batchId}/row-2`);
  const evidence = await page.evaluate(async ({ batchId, expectedOwner }) => {
    const require = (condition, message) => { if (!condition) throw new Error(message); };
    const read = async path => {
      const response = await fetch(`/api/batches/${batchId}${path}`, { cache: "no-store" });
      require(response.status === 200, `Read failed: ${path}`);
      return response.json();
    };
    const summary = await read("");
    require(summary.state === "completed", "Worker not finished; resume after execution, do not resubmit");
    const machineHashes = {};
    for (const [row, itemId, mpn] of [[2, "001", "PART-1"], [3, "002", "PART-2"]]) {
      const detail = await read(`/items/row-${row}`);
      require(detail.machine_result.model_call_status === "not_attempted", "Unexpected model execution");
      require(detail.machine_result.manifest.product.item_id === itemId && detail.machine_result.manifest.product.mpn === mpn, "Wrong synthetic product");
      require(detail.machine_result.attributes.every(attribute => attribute.candidates.length === 0), "Unexpected synthetic candidate");
      require(detail.provenance.length > 0 && detail.provenance.every(source => source.parsing === "cache" && source.parse_origin === "synthetic_fixture_not_service_analysis"), "Synthetic cache provenance missing");
      machineHashes[String(row)] = detail.machine_sha256;
    }
    const review = { attribute_id: "Pressure Rating", decision: "reject", reason: "Synthetic acceptance only: no candidate; not a product decision" };
    const post = payload => fetch(`/api/batches/${batchId}/items/row-2/reviews`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    require((await post({ ...review, reviewer: "spoofed" })).status === 422, "Spoofed reviewer accepted");
    require((await post(review)).status === 200, "Verified review failed");
    require((await post(review)).status === 422, "Duplicate review accepted");
    const after = await read("/items/row-2");
    require(after.machine_sha256 === machineHashes["2"], "Review mutated machine result");
    require((await read("/items/row-3")).machine_sha256 === machineHashes["3"], "Review mutated second product");
    require(after.reviewer_identity === "verified_entra", "Reviewer is not verified Entra");
    const reviewed = after.reviewed_result.attributes.find(attribute => attribute.attribute_id === "Pressure Rating");
    require(reviewed.review.reviewer === expectedOwner && reviewed.review.reviewed_at, "Server reviewer identity/timestamp absent");
    const exported = await fetch(`/api/batches/${batchId}/export`, { cache: "no-store" });
    require(exported.status === 200 && exported.headers.get("cache-control").includes("no-store"), "Export failed or cache enabled");
    const bytes = new Uint8Array(await exported.arrayBuffer());
    let binary = "";
    for (const byte of bytes) binary += String.fromCharCode(byte);
    return { batchId, machineHashes, verifiedOwner: expectedOwner, exportBase64: btoa(binary), exportBytes: bytes.length, liveAI: false };
  }, { batchId, expectedOwner });
  verifyExportWorkbook(evidence, python);
  const { exportBase64, ...summary } = evidence;
  return { ...summary, exportSha256: (await import("node:crypto")).createHash("sha256").update(Buffer.from(exportBase64, "base64")).digest("hex"), workbookCellsVerified: true };
}
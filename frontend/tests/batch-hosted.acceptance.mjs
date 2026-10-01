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

export async function reviewAndExport(page, { origin, batchId, expectedOwner, approved }) {
  if (approved !== "hosted-synthetic-only" || !origin.startsWith("https://")) throw new Error("Approved hosted synthetic acceptance required");
  await page.goto(`${origin}/batches/${batchId}/row-2`);
  return page.evaluate(async ({ batchId, expectedOwner }) => {
    const require = (condition, message) => { if (!condition) throw new Error(message); };
    const read = async path => {
      const response = await fetch(`/api/batches/${batchId}${path}`, { cache: "no-store" });
      require(response.status === 200, `Read failed: ${path}`);
      return response.json();
    };
    const summary = await read("");
    require(summary.state === "completed", "Worker not finished; resume after execution, do not resubmit");
    const before = await read("/items/row-2");
    require(before.machine_result.model_call_status === "not_attempted", "Unexpected model execution");
    require(before.provenance.some(source => source.parsing === "cache" && source.parse_origin === "synthetic_fixture_not_service_analysis"), "Synthetic cache provenance missing");
    const review = { attribute_id: "Pressure Rating", decision: "reject", reason: "Synthetic acceptance only: no candidate; not a product decision" };
    const post = payload => fetch(`/api/batches/${batchId}/items/row-2/reviews`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    require((await post({ ...review, reviewer: "spoofed" })).status === 422, "Spoofed reviewer accepted");
    require((await post(review)).status === 200, "Verified review failed");
    require((await post(review)).status === 422, "Duplicate review accepted");
    const after = await read("/items/row-2");
    require(after.machine_sha256 === before.machine_sha256, "Review mutated machine result");
    require(after.reviewer_identity === "verified_entra", "Reviewer is not verified Entra");
    const reviewed = after.reviewed_result.attributes.find(attribute => attribute.attribute_id === "Pressure Rating");
    require(reviewed.review.reviewer === expectedOwner && reviewed.review.reviewed_at, "Server reviewer identity/timestamp absent");
    const exported = await fetch(`/api/batches/${batchId}/export`, { cache: "no-store" });
    require(exported.status === 200 && exported.headers.get("cache-control").includes("no-store"), "Export failed or cache enabled");
    const bytes = new Uint8Array(await exported.arrayBuffer());
    require(bytes[0] === 80 && bytes[1] === 75, "Export is not a ZIP/XLSX");
    return { batchId, machineSha256: before.machine_sha256, verifiedOwner: expectedOwner, exportBytes: bytes.length, liveAI: false };
  }, { batchId, expectedOwner });
}
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";
import { observeSlice, reviewAndExport, verifyOtherOwner } from "./batch-hosted.acceptance.mjs";

const require = createRequire(import.meta.url);
const { NextRequest } = require("next/server");
const compiledModule = { exports: {} };
const source = readFileSync(new URL("../middleware.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
new Function("require", "module", "exports", compiled)(require, compiledModule, compiledModule.exports);

test("hosted middleware blocks legacy data routes even with loopback headers", () => {
  const previous = process.env.CONTAINER_APP_NAME;
  process.env.CONTAINER_APP_NAME = "synthetic-hosted";
  try {
    for (const path of ["/api/gallery", "/api/environment", "/api/pilot/runs", "/api/metadata"]) {
      const request = new NextRequest(`http://localhost:3100${path}`, { headers: { host: "localhost:3100", "x-docintel-development": "1" } });
      assert.equal(compiledModule.exports.middleware(request).status, 404);
    }
    for (const path of ["/api/auth/session", "/api/batches"]) {
      assert.equal(compiledModule.exports.middleware(new NextRequest(`https://synthetic.invalid${path}`)).status, 200);
    }
  } finally {
    if (previous === undefined) delete process.env.CONTAINER_APP_NAME;
    else process.env.CONTAINER_APP_NAME = previous;
  }
});

test("hosted acceptance requires workbook verification before any review", async () => {
  await assert.rejects(reviewAndExport({}, { origin: "https://synthetic.invalid", approved: "hosted-synthetic-only" }), /Python executable/);
});

test("hosted acceptance rejects a wrong second product before writing a review", async context => {
  const requests = [];
  context.mock.method(globalThis, "fetch", async path => {
    requests.push(path);
    if (path.endsWith("/batch")) return { status: 200, json: async () => ({ state: "completed" }) };
    const second = path.endsWith("row-3");
    return { status: 200, json: async () => ({ machine_sha256: "synthetic", machine_result: { model_call_status: "not_attempted", manifest: { product: { item_id: second ? "WRONG" : "001", mpn: second ? "PART-2" : "PART-1" } }, attributes: [] }, provenance: [{ parsing: "cache", parse_origin: "synthetic_fixture_not_service_analysis" }] }) };
  });
  const page = { goto: async () => {}, evaluate: async (callback, args) => callback(args) };
  await assert.rejects(reviewAndExport(page, { origin: "https://synthetic.invalid", batchId: "batch", expectedOwner: "synthetic-owner", approved: "hosted-synthetic-only", python: "not-executed" }), /Wrong synthetic product/);
  assert.equal(requests.length, 3);
  assert(requests.every(path => !path.endsWith("/reviews")));
});

test("slice observation requires stable completed-product evidence", async context => {
  context.mock.method(globalThis, "fetch", async path => ({ status: 200, json: async () => path.endsWith("row-2") ? { machine_result: { model_call_status: "not_attempted" }, machine_sha256: "original" } : { state: "queued", progress: { finished: 1 } } }));
  const page = { goto: async () => {}, evaluate: async (callback, args) => callback(args) };
  const options = { origin: "https://synthetic.invalid", batchId: "batch", finished: 1, approved: "hosted-synthetic-only" };
  assert.equal((await observeSlice(page, options)).firstMachineHash, "original");
  await assert.rejects(observeSlice(page, { ...options, firstMachineHash: "changed" }), /changed/);
});

test("owner-isolation helper verifies a second authenticated identity", async context => {
  context.mock.method(globalThis, "fetch", async path => path.endsWith("/catalog") ? { status: 200, json: async () => ({ identity: "second-owner" }) } : { status: 404 });
  const page = { goto: async () => {}, evaluate: async (callback, args) => callback(args) };
  const options = { origin: "https://synthetic.invalid", batchId: "batch", expectedOwner: "second-owner", approved: "hosted-synthetic-only" };
  assert.deepEqual(await verifyOtherOwner(page, options), { ownerIsolation: true });
  await assert.rejects(verifyOtherOwner(page, { ...options, expectedOwner: "wrong-owner" }), /owner mismatch/);
});

test("private source upload proxy preserves authentication, origin and route bounds", async context => {
  const environment = { NODE_ENV: "production", DOCINTEL_PORTAL_ORIGIN: "https://portal.invalid", DOCINTEL_BATCH_API_URL: "https://backend.invalid/api/v1/batches" };
  const previous = Object.fromEntries(Object.keys(environment).map(key => [key, process.env[key]]));
  Object.assign(process.env, environment);
  let token = { batchAccessToken: "synthetic-token", batchExpiresAt: Date.now() / 1000 + 60 };
  const routeModule = { exports: {} };
  const routeSource = readFileSync(new URL("../app/api/batches/[[...path]]/route.ts", import.meta.url), "utf8");
  const routeCode = ts.transpileModule(routeSource, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const mockRequire = name => name === "next-auth/jwt" ? { getToken: async () => token } : require(name);
  new Function("require", "module", "exports", routeCode)(mockRequire, routeModule, routeModule.exports);
  const requests = [];
  context.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return new Response('{"verified":true}', { headers: { "content-type": "application/json" } });
  });
  const post = (path, origin = environment.DOCINTEL_PORTAL_ORIGIN) => routeModule.exports.POST(
    new NextRequest(`${environment.DOCINTEL_PORTAL_ORIGIN}/api/batches/${path.join("/")}`, { method: "POST", headers: { origin }, body: "synthetic-bytes" }),
    { params: Promise.resolve({ path }) },
  );
  try {
    assert.equal((await post(["pilot-sources", "approved-source"])).status, 200);
    assert.equal(requests[0].url, "https://backend.invalid/api/v1/batches/pilot-sources/approved-source");
    assert.equal(requests[0].options.headers.Authorization, "Bearer synthetic-token");
    assert.equal(requests[0].options.redirect, "error");
    assert.equal(requests[0].options.body.toString(), "synthetic-bytes");
    assert.equal((await post(["pilot-sources", "finalize"])).status, 200);
    assert.equal((await post(["pilot-sources", ".."])).status, 404);
    assert.equal((await post(["pilot-sources", "approved-source", "extra"])).status, 404);
    assert.equal((await post(["pilot-sources", "approved-source"], "https://untrusted.invalid")).status, 403);
    token = null;
    assert.equal((await post(["pilot-sources", "approved-source"])).status, 401);
    assert.equal(requests.length, 2);
  } finally {
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
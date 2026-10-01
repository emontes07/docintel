import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import ts from "typescript";

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
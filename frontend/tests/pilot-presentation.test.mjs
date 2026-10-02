import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

const source = fs.readFileSync(new URL('../utils/pilot-presentation.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText;
const exports = {};
vm.runInNewContext(compiled, { exports, URL, URLSearchParams });
const { stagePresentation, sourceLabel, executionLabel } = exports;

test('recorded parsing success is distinct from method; failed and unknown stay honest', () => {
  for (const [status, method] of [['live', 'Fresh analysis'], ['cached', 'Cache']]) {
    const parsing = { status, origin: 'recorded-origin' };
    assert.equal(stagePresentation('parsing', status, parsing).outcome, 'succeeded');
    assert.equal(stagePresentation('parsing', status, parsing).method, method);
    assert.equal(stagePresentation('parsing', 'failed', parsing).successful, false);
  }
  assert.equal(stagePresentation('parsing', 'live', null).outcome, 'unknown');
  assert.equal(stagePresentation('parsing', 'future-status', null).successful, false);
  assert.equal(stagePresentation('inference', 'replayed', null).method, 'Replayed output; no new inference');
  assert.equal(executionLabel('unexpected'), 'Unknown execution method');
});

test('source labels expose filenames and mapped locations, not machine directories', () => {
  const original = '/private/customer/' + 'long-name-'.repeat(40) + '.pdf#page=1&paragraph=43';
  assert.equal(sourceLabel(original).filename, 'long-name-'.repeat(40) + '.pdf');
  assert.equal(sourceLabel(original).position, 'Page: 1 · Paragraph: 43');
  assert.equal(sourceLabel('https://example.invalid/folder/a%20b.pdf#page=2&table=0&row=1&column=2').filename, 'a b.pdf');
  assert.equal(sourceLabel('relative.pdf').position, 'Location not recorded');
});
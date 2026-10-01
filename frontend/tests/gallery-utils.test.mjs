import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import ts from 'typescript';

function mapper(items) {
  const source = fs.readFileSync(new URL('../utils/gallery-utils.ts', import.meta.url), 'utf8');
  const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  vm.runInNewContext(compiled, {
    exports,
    console: { log() {}, warn() {}, error() {} },
    require(name) {
      if (name === '@/services/api') return { MediaType: { IMAGE: 'image' }, fetchGalleryImages: async () => ({ success: true, items }) };
      if (name === '@/services/sas-token') return { sasTokenService: { getBlobUrl: async name => `https://example.invalid/${name}` } };
      throw new Error('Unexpected external dependency');
    },
  });
  return exports;
}

test('structured gallery analysis retains values, dimensions, and analyzed status', async () => {
  const item = { id: 'test', name: 'test_image.png', media_type: 'image', metadata: { prompt: 'test title', width: 800, height: 600, has_analysis: true, analysis: { summary: 'Test summary', products: 'Test product', feedback: 'Test feedback', tags: [' alpha ', 'beta'] } } };
  const [image] = await mapper([item]).fetchImages();
  assert.equal(image.title, 'Test title');
  assert.equal(image.description, 'Test summary');
  assert.equal(image.width, 800);
  assert.equal(image.analysis.analyzed, true);
  assert.deepEqual(Array.from(image.tags), ['alpha', 'beta']);
  assert.equal(image.analysis.products, 'Test product');
});

test('gallery metadata without structured analysis still maps safely', async () => {
  const [image] = await mapper([{ id: 'test', name: 'test_image.png', media_type: 'image', metadata: { description: 'Plain description', tags: 'one; two' } }]).fetchImages();
  assert.equal(image.title, 'Test image');
  assert.equal(image.description, 'Plain description');
  assert.equal(image.analysis, undefined);
  assert.deepEqual(Array.from(image.tags), ['one', 'two']);
});
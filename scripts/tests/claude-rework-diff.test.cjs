const { test } = require('node:test');
const assert = require('node:assert/strict');
const { parseNameStatus, buildManifest, MAX_TOTAL_CONTENT_BYTES } = require('../claude-rework-diff.cjs');

test('parses added/modified/deleted lines from git diff --name-status', () => {
  const entries = parseNameStatus('A\ta.txt\nM\tb.txt\nD\tc.txt\n');
  assert.deepEqual(entries, [
    { action: 'upsert', path: 'a.txt' },
    { action: 'upsert', path: 'b.txt' },
    { action: 'delete', path: 'c.txt' },
  ]);
});
test('ignores blank lines and trims trailing newline noise', () => {
  const entries = parseNameStatus('\nA\ta.txt\n\n');
  assert.deepEqual(entries, [{ action: 'upsert', path: 'a.txt' }]);
});
test('empty diff yields no entries', () => {
  assert.deepEqual(parseNameStatus(''), []);
  assert.deepEqual(parseNameStatus(null), []);
});
test('builds a manifest with base64 content for upserts and no content for deletes', () => {
  const entries = [{ action: 'upsert', path: 'a.txt' }, { action: 'delete', path: 'b.txt' }];
  const manifest = buildManifest({
    entries, readFileBase64: (p) => Buffer.from(`content of ${p}`).toString('base64'),
    commitMessage: 'Fix the thing',
  });
  assert.equal(manifest.changes.length, 2);
  assert.equal(manifest.changes[0], manifest.changes[0]);
  assert.equal(manifest.changes[0].action, 'upsert');
  assert.equal(Buffer.from(manifest.changes[0].content, 'base64').toString(), 'content of a.txt');
  assert.equal(manifest.changes[1].action, 'delete');
  assert.equal(manifest.changes[1].content, undefined);
  assert.equal(manifest.commitMessage, 'Fix the thing');
});
test('null commit message is preserved as null, not coerced to empty string', () => {
  const manifest = buildManifest({ entries: [], readFileBase64: () => '', commitMessage: null });
  assert.equal(manifest.commitMessage, null);
});
test('rejects a path traversal entry rather than silently including it', () => {
  assert.throws(
    () => buildManifest({ entries: [{ action: 'upsert', path: '../outside.txt' }], readFileBase64: () => 'x' }),
    /unsafe path/,
  );
});
test('fails closed once total changed-file content exceeds the safety limit', () => {
  const big = 'x'.repeat(MAX_TOTAL_CONTENT_BYTES + 1);
  assert.throws(
    () => buildManifest({ entries: [{ action: 'upsert', path: 'big.txt' }], readFileBase64: () => big }),
    /safety limit/,
  );
});

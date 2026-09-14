const { test } = require('node:test');
const assert = require('node:assert/strict');
const createPR = require('../claude-create-pr.cjs');

function fixture({ head = [], open = [], ahead = 1, files = [{}] } = {}) {
  const calls = [];
  const github = {
    rest: {
      pulls: { list: 'list', create: async (args) => { calls.push(args); } },
      repos: { compareCommitsWithBasehead: async (args) => {
        assert.equal(args.basehead, 'main...claude/issue-89-test');
        return { data: { ahead_by: ahead, files } };
      } },
    },
    paginate: async (_, args) => args.state === 'all' ? head : open,
  };
  return { calls, github, branch: 'claude/issue-89-test',
    context: { repo: { owner: 'wayne19870129', repo: 'lingsway-platform' },
      payload: { issue: { number: 89, title: 'Test `literal` $(text)',
        html_url: 'https://github.com/wayne19870129/lingsway-platform/issues/89' } },
      serverUrl: 'https://github.com', runId: 123 },
    core: { info() {} } };
}
test('creates one main PR with literal title, safe body and no closing keyword', async () => {
  const f = fixture(); await createPR(f);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].base, 'main');
  assert.equal(f.calls[0].head, f.branch);
  assert.equal(f.calls[0].title, 'Issue #89: Test `literal` $(text)');
  assert.doesNotMatch(f.calls[0].body, /(?:closes|fixes|resolves)\s+#89/i);
});
for (const [name, options] of [
  ['no commits', { ahead: 0 }], ['no net file diff', { files: [] }],
  ['existing head including closed PR', { head: [{ number: 90 }] }],
  ['same Issue branch', { open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] }],
  ['manual PR mentioning Issue', { open: [{ number: 90, head: { ref: 'manual' }, body: 'Related #89.' }] }],
  ['manual PR using Issue URL', { open: [{ number: 90, head: { ref: 'manual' },
    body: 'https://github.com/wayne19870129/lingsway-platform/issues/89' }] }],
]) test(name, async () => { const f = fixture(options); await createPR(f); assert.equal(f.calls.length, 0); });
test('different Issue number does not block', async () => {
  const f = fixture({ open: [{ head: { ref: 'manual' }, body: 'Related #890. https://github.com/wayne19870129/lingsway-platform/issues/890' }] });
  await createPR(f); assert.equal(f.calls.length, 1);
});
test('empty branch and PR comments do nothing', async () => {
  const f = fixture(); f.branch = ''; await createPR(f);
  f.branch = 'claude/issue-89-test'; f.context.payload.issue.pull_request = {};
  await createPR(f); assert.equal(f.calls.length, 0);
});
test('wrong Issue branch rejected', async () => {
  const f = fixture(); f.branch = 'claude/issue-890-test';
  await assert.rejects(createPR(f), /Unexpected/);
});
test('API failures fail closed instead of creating', async () => {
  const f = fixture(); f.github.paginate = async () => { throw new Error('403'); };
  await assert.rejects(createPR(f), /403/); assert.equal(f.calls.length, 0);
});
test('serialized second Issue branch observes the first PR', async () => {
  const f = fixture(); await createPR(f);
  const next = fixture({ open: [{ number: 91, head: { ref: f.branch,
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] });
  await createPR(next); assert.equal(next.calls.length, 0);
});

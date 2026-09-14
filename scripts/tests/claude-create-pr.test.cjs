const { test } = require('node:test');
const assert = require('node:assert/strict');
const createPR = require('../claude-create-pr.cjs');

function fixture({ head = [], open = [], ahead = 1, files = [{ filename: 'a.txt' }],
  commits = [{ commit: { message: 'Do the thing' } }], mergeError = null, updateError = null } = {}) {
  const creates = []; const merges = []; const updates = [];
  const github = {
    rest: {
      pulls: {
        list: 'list',
        create: async (args) => { creates.push(args); },
        update: async (args) => { if (updateError) throw updateError; updates.push(args); },
      },
      repos: {
        compareCommitsWithBasehead: async (args) => {
          assert.equal(args.basehead, 'main...claude/issue-89-test');
          return { data: { ahead_by: ahead, files, commits } };
        },
        merge: async (args) => { if (mergeError) throw mergeError; merges.push(args); },
      },
    },
    paginate: async (_, args) => args.state === 'all' ? head : open,
  };
  return { creates, merges, updates, github, branch: 'claude/issue-89-test',
    context: { repo: { owner: 'wayne19870129', repo: 'lingsway-platform' },
      payload: { issue: { number: 89, title: 'Test `literal` $(text)',
        html_url: 'https://github.com/wayne19870129/lingsway-platform/issues/89' } },
      serverUrl: 'https://github.com', runId: 123 },
    core: { info() {}, warning() {} } };
}
test('creates one main PR with literal title, safe body and no closing keyword', async () => {
  const f = fixture(); await createPR(f);
  assert.equal(f.creates.length, 1);
  assert.equal(f.creates[0].base, 'main');
  assert.equal(f.creates[0].head, f.branch);
  assert.equal(f.creates[0].title, 'Issue #89: Test `literal` $(text)');
  assert.doesNotMatch(f.creates[0].body, /(?:closes|fixes|resolves)\s+#89/i);
  assert.match(f.creates[0].body, /Do the thing/);
  assert.match(f.creates[0].body, /`a\.txt`/);
});
for (const [name, options] of [
  ['no commits', { ahead: 0 }], ['no net file diff', { files: [] }],
  ['existing head including closed PR', { head: [{ number: 90 }] }],
]) test(name, async () => {
  const f = fixture(options); await createPR(f);
  assert.equal(f.creates.length, 0); assert.equal(f.merges.length, 0); assert.equal(f.updates.length, 0);
});
test('different Issue number does not block', async () => {
  const f = fixture({ open: [{ head: { ref: 'manual' }, body: 'Related #890. https://github.com/wayne19870129/lingsway-platform/issues/890' }] });
  await createPR(f); assert.equal(f.creates.length, 1);
});
test('empty branch and PR comments do nothing', async () => {
  const f = fixture(); f.branch = ''; await createPR(f);
  f.branch = 'claude/issue-89-test'; f.context.payload.issue.pull_request = {};
  await createPR(f); assert.equal(f.creates.length, 0);
});
test('wrong Issue branch rejected', async () => {
  const f = fixture(); f.branch = 'claude/issue-890-test';
  await assert.rejects(createPR(f), /Unexpected/);
});
test('API failures fail closed instead of creating', async () => {
  const f = fixture(); f.github.paginate = async () => { throw new Error('403'); };
  await assert.rejects(createPR(f), /403/); assert.equal(f.creates.length, 0);
});
test('local branch never pushed creates no PR', async () => {
  const f = fixture(); f.github.rest.repos.compareCommitsWithBasehead = async () => {
    throw Object.assign(new Error('Not Found'), { status: 404 });
  };
  await createPR(f); assert.equal(f.creates.length, 0);
});
test('comparison authorization failure is not treated as empty branch', async () => {
  const f = fixture(); f.github.rest.repos.compareCommitsWithBasehead = async () => {
    throw Object.assign(new Error('Forbidden'), { status: 403 });
  };
  await assert.rejects(createPR(f), /Forbidden/); assert.equal(f.creates.length, 0);
});
test('serialized second Issue branch observes the first PR and does not merge/update', async () => {
  const f = fixture(); await createPR(f);
  const next = fixture({ open: [{ number: 91, head: { ref: f.branch,
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] });
  await createPR(next);
  assert.equal(next.creates.length, 0); assert.equal(next.merges.length, 0); assert.equal(next.updates.length, 0);
});
test('same-Issue follow-up work is merged into the existing own-repo PR branch, not orphaned', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.merges.length, 1);
  assert.equal(f.merges[0].base, 'claude/issue-89-other');
  assert.equal(f.merges[0].head, f.branch);
  assert.equal(f.updates.length, 0);
});
test('a merge conflict falls back to recording a pending-branch note, not silent loss', async () => {
  const f = fixture({
    open: [{ number: 90, head: { ref: 'claude/issue-89-other',
      repo: { full_name: 'wayne19870129/lingsway-platform' } } }],
    mergeError: Object.assign(new Error('Merge conflict'), { status: 409 }),
  });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 1);
  assert.equal(f.updates[0].pull_number, 90);
  assert.match(f.updates[0].body, /claude-pending-branch:claude\/issue-89-test/);
  assert.match(f.updates[0].body, /not lost/);
});
test('a manual same-Issue PR on another repo/fork records a note instead of merging', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual' }, body: 'Related #89.' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.merges.length, 0);
  assert.equal(f.updates.length, 1);
  assert.match(f.updates[0].body, /claude-pending-branch:claude\/issue-89-test/);
});
test('a manual same-Issue PR matched by Issue URL also records a note', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual' },
    body: 'https://github.com/wayne19870129/lingsway-platform/issues/89' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 1);
});
test('does not duplicate the pending-branch note on rerun', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual' },
    body: 'Related #89.\n\n<!-- claude-pending-branch:claude/issue-89-test -->\nalready noted' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 0);
});

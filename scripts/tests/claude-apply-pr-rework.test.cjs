const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const applyPrRework = require('../claude-apply-pr-rework.cjs');

function fixture({
  pr = {
    state: 'open',
    head: { ref: 'claude/issue-95-20260914-0000', sha: 'base-sha', repo: { full_name: 'wayne19870129/lingsway-platform' } },
  },
  manifest = { changes: [{ action: 'upsert', path: 'docs/example.md', content: Buffer.from('hi').toString('base64') }], commitMessage: 'Fix per review' },
  prNumber = 94,
  branch = 'claude/issue-95-20260914-0000',
  baseSha = 'base-sha',
  updateRefError = null,
} = {}) {
  const blobs = []; const trees = []; const commits = []; const refUpdates = [];
  const github = {
    rest: {
      pulls: { get: async () => ({ data: pr }) },
      git: {
        getCommit: async () => ({ data: { tree: { sha: 'base-tree-sha' } } }),
        createBlob: async (args) => { const sha = `blob-${blobs.length}`; blobs.push(args); return { data: { sha } }; },
        createTree: async (args) => { trees.push(args); return { data: { sha: 'new-tree-sha' } }; },
        createCommit: async (args) => { commits.push(args); return { data: { sha: 'new-commit-sha' } }; },
        updateRef: async (args) => { if (updateRefError) throw updateRefError; refUpdates.push(args); },
      },
    },
  };
  const context = { repo: { owner: 'wayne19870129', repo: 'lingsway-platform' } };
  const core = { info() {} };
  return { github, context, core, manifest, prNumber, branch, baseSha, blobs, trees, commits, refUpdates };
}

test('trusted automation-owned PR update succeeds', async () => {
  const f = fixture();
  await applyPrRework(f);
  assert.equal(f.blobs.length, 1);
  assert.equal(f.trees.length, 1);
  assert.equal(f.trees[0].base_tree, 'base-tree-sha');
  assert.equal(f.commits.length, 1);
  assert.equal(f.commits[0].parents[0], 'base-sha');
  assert.equal(f.commits[0].message, 'Fix per review');
  assert.equal(f.refUpdates.length, 1);
  assert.equal(f.refUpdates[0].ref, 'heads/claude/issue-95-20260914-0000');
  assert.equal(f.refUpdates[0].force, false);
});

test('falls back to a synthesized commit message when none was captured', async () => {
  const f = fixture({ manifest: { changes: [{ action: 'upsert', path: 'a.txt', content: 'aGk=' }], commitMessage: null } });
  await applyPrRework(f);
  assert.match(f.commits[0].message, /Rework PR #94/);
});

test('a deletion entry is written with a null blob sha', async () => {
  const f = fixture({ manifest: { changes: [{ action: 'delete', path: 'old.txt' }] } });
  await applyPrRework(f);
  assert.equal(f.blobs.length, 0);
  assert.equal(f.trees[0].tree[0].sha, null);
  assert.equal(f.trees[0].tree[0].path, 'old.txt');
});

test('stale starting SHA fails closed without any write call', async () => {
  const f = fixture({ pr: { state: 'open', head: { ref: 'claude/issue-95-20260914-0000', sha: 'newer-sha', repo: { full_name: 'wayne19870129/lingsway-platform' } } } });
  await assert.rejects(applyPrRework(f), /Stale transfer/);
  assert.equal(f.blobs.length, 0);
  assert.equal(f.refUpdates.length, 0);
});

test('a concurrent head change during the write is never overwritten', async () => {
  const f = fixture({ updateRefError: Object.assign(new Error('reference update failed'), { status: 422 }) });
  await assert.rejects(applyPrRework(f), /Stale transfer.*moved concurrently/);
});

test('an unrelated ref-update error is not swallowed as a stale race', async () => {
  const f = fixture({ updateRefError: Object.assign(new Error('Forbidden'), { status: 403 }) });
  await assert.rejects(applyPrRework(f), /Forbidden/);
});

test('a fork PR is rejected before any API write', async () => {
  const f = fixture({ pr: { state: 'open', head: { ref: 'claude/issue-95-20260914-0000', sha: 'base-sha', repo: { full_name: 'someone-else/lingsway-platform' } } } });
  await assert.rejects(applyPrRework(f), /Refusing to write.*not this repository/);
  assert.equal(f.blobs.length, 0);
});

test('a same-repo human/non-automation-owned branch is rejected by naming policy alone, before any API call', async () => {
  const f = fixture({ branch: 'wayne-manual-fix', pr: { state: 'open', head: { ref: 'wayne-manual-fix', sha: 'base-sha', repo: { full_name: 'wayne19870129/lingsway-platform' } } } });
  let getCalled = false;
  f.github.rest.pulls.get = async () => { getCalled = true; return { data: f.github.rest.pulls.get }; };
  await assert.rejects(applyPrRework(f), /naming policy/);
  assert.equal(getCalled, false);
});

test('a closed PR is rejected', async () => {
  const f = fixture({ pr: { state: 'closed', head: { ref: 'claude/issue-95-20260914-0000', sha: 'base-sha', repo: { full_name: 'wayne19870129/lingsway-platform' } } } });
  await assert.rejects(applyPrRework(f), /is not open/);
});

test('a head ref that no longer matches the expected branch is rejected', async () => {
  const f = fixture({ pr: { state: 'open', head: { ref: 'claude/issue-95-20260914-9999', sha: 'base-sha', repo: { full_name: 'wayne19870129/lingsway-platform' } } } });
  await assert.rejects(applyPrRework(f), /no longer matches expected branch/);
});

for (const [name, manifest] of [
  ['missing changes array', {}],
  ['a path traversal entry', { changes: [{ action: 'upsert', path: '../outside.txt', content: 'aGk=' }] }],
  ['an absolute path entry', { changes: [{ action: 'upsert', path: '/etc/passwd', content: 'aGk=' }] }],
  ['an unknown action', { changes: [{ action: 'wipe', path: 'a.txt' }] }],
  ['an upsert missing content', { changes: [{ action: 'upsert', path: 'a.txt' }] }],
]) test(`malformed or conflicting transfer fails closed: ${name}`, async () => {
  const f = fixture({ manifest });
  await assert.rejects(applyPrRework(f));
  assert.equal(f.blobs.length, 0);
  assert.equal(f.refUpdates.length, 0);
});

test('missing prNumber/branch/baseSha fails closed', async () => {
  const f = fixture({ prNumber: null });
  await assert.rejects(applyPrRework(f), /Malformed transfer/);
});

test('an empty change set is a no-op, not an error', async () => {
  const f = fixture({ manifest: { changes: [] } });
  await applyPrRework(f);
  assert.equal(f.blobs.length, 0);
  assert.equal(f.refUpdates.length, 0);
});

test('the module never touches a raw token value; it only uses the pre-authenticated github client', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'claude-apply-pr-rework.cjs'), 'utf8');
  assert.doesNotMatch(source, /process\.env/, 'must never read a secret out of the environment itself');
  assert.doesNotMatch(source, /ghp_|github_pat_/, 'must never embed a literal token value');
});

test('the trusted writer and existing create-pr jobs are the only ones given CLAUDE_PR_TOKEN; the interactive Claude and diff-capture jobs never receive it', () => {
  const workflow = fs.readFileSync(
    path.join(__dirname, '..', '..', '.github', 'workflows', 'claude.yml'), 'utf8');
  const jobStarts = ['claude', 'create-pr', 'claude-rework', 'apply-pr-rework']
    .map((name) => ({ name, index: workflow.indexOf(`\n  ${name}:\n`) }));
  for (const { name, index } of jobStarts) assert.ok(index >= 0, `expected job '${name}' in workflow`);
  jobStarts.sort((a, b) => a.index - b.index);
  const bounds = jobStarts.map((job, i) => ({
    name: job.name, text: workflow.slice(job.index, jobStarts[i + 1] ? jobStarts[i + 1].index : workflow.length),
  }));
  // Match only an actual credential wire-up (github-token/PR_TOKEN_CONFIGURED
  // referencing the secret), not prose mentioning the secret's name in a
  // comment - a design-rationale comment for one job can otherwise land
  // inside a neighboring job's slice depending on where it's placed in the file.
  const patWireUp = /(?:github-token|PR_TOKEN_CONFIGURED):\s*\$\{\{\s*secrets\.CLAUDE_PR_TOKEN/;
  const expectedPatHolders = new Set(['create-pr', 'apply-pr-rework']);
  for (const { name, text } of bounds) {
    if (expectedPatHolders.has(name)) {
      assert.match(text, patWireUp, `job '${name}' should be a consumer of the PAT`);
    } else {
      assert.doesNotMatch(text, patWireUp, `job '${name}' must never receive the PR/branch-write PAT`);
    }
  }
});

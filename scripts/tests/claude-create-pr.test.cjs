const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const createPR = require('../claude-create-pr.cjs');

function fixture({ head = [], open = [], ahead = 1, files = [{ filename: 'a.txt' }],
  commits = [{ commit: { message: 'Do the thing' } }], updateError = null,
  mergeError = null } = {}) {
  const creates = []; const updates = []; const merges = [];
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
  return { creates, updates, merges, github, branch: 'claude/issue-89-test',
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
  assert.equal(f.creates.length, 0); assert.equal(f.updates.length, 0);
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
test('serialized rerun of the same branch observes the first PR and does not update', async () => {
  const f = fixture(); await createPR(f);
  const next = fixture({ open: [{ number: 91, head: { ref: f.branch,
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] });
  await createPR(next);
  assert.equal(next.creates.length, 0); assert.equal(next.updates.length, 0);
});
test('same-Issue follow-up on an automation-owned branch merges into the existing PR head', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 0);
  assert.equal(f.merges.length, 1);
  assert.equal(f.merges[0].base, 'claude/issue-89-other');
  assert.equal(f.merges[0].head, f.branch);
});
test('a merge conflict on an automation-owned branch falls back to a pending-branch pointer', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }],
    mergeError: Object.assign(new Error('Conflict'), { status: 409 }) });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 1);
  assert.equal(f.updates[0].pull_number, 90);
  assert.match(f.updates[0].body, /claude-pending-branch:claude\/issue-89-test/);
  assert.match(f.updates[0].body, /could not be merged into this PR's head automatically/);
});
test('a missing-branch merge failure on an automation-owned branch falls back to a pointer', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }],
    mergeError: Object.assign(new Error('Not Found'), { status: 404 }) });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 1);
  assert.equal(f.merges.length, 0);
});
test('an unexpected merge error is not swallowed into a pointer', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'wayne19870129/lingsway-platform' } } }],
    mergeError: Object.assign(new Error('Forbidden'), { status: 403 }) });
  await assert.rejects(createPR(f), /Forbidden/);
  assert.equal(f.updates.length, 0);
});
test('a same-Issue PR matched only by body text is never merged into, only pointed at', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual-branch',
    repo: { full_name: 'wayne19870129/lingsway-platform' } }, body: 'Fixes #89 manually.' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.merges.length, 0);
  assert.equal(f.updates.length, 1);
  assert.equal(f.updates[0].pull_number, 90);
  assert.match(f.updates[0].body, /is not a branch this automation created/);
});
test('a same-Issue fork-headed PR is never merged into, only pointed at', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'claude/issue-89-other',
    repo: { full_name: 'someone-else/lingsway-platform' } }, body: 'Related #89.' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.merges.length, 0);
  assert.equal(f.updates.length, 1);
});
test('a manual same-Issue PR records a note instead of creating another PR', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual' }, body: 'Related #89.' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
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
test('exact standalone Closes line in a commit adds the real closing keyword', async () => {
  const f = fixture({ commits: [{ commit: { message: 'Do the thing' } },
    { commit: { message: 'Fix wording\n\nCloses #89' } }] });
  await createPR(f);
  assert.match(f.creates[0].body, /(?:^|\n)Closes #89(?:\n|$)/);
});
test('closing keyword embedded in prose is not trusted', async () => {
  const f = fixture({ commits: [{ commit: { message: 'Says Closes #89 in passing' } }] });
  await createPR(f);
  assert.doesNotMatch(f.creates[0].body, /(?:^|\n)Closes #89(?:\n|$)/);
  assert.match(f.creates[0].body, /stays open/);
});
test('closing keyword for a different issue number is not trusted', async () => {
  const f = fixture({ commits: [{ commit: { message: 'Closes #90' } }] });
  await createPR(f);
  assert.doesNotMatch(f.creates[0].body, /Closes #\d/);
  assert.match(f.creates[0].body, /stays open/);
});
test('does not duplicate the pending-branch note on rerun', async () => {
  const f = fixture({ open: [{ number: 90, head: { ref: 'manual' },
    body: 'Related #89.\n\n<!-- claude-pending-branch:claude/issue-89-test -->\nalready noted' }] });
  await createPR(f);
  assert.equal(f.creates.length, 0);
  assert.equal(f.updates.length, 0);
});
test('the trusted PR-write/branch-write token is never given to the Claude step', () => {
  const workflow = fs.readFileSync(
    path.join(__dirname, '..', '..', '.github', 'workflows', 'claude.yml'), 'utf8');
  const claudeJobStart = workflow.indexOf('\n  claude:\n');
  const createPrJobStart = workflow.indexOf('\n  create-pr:\n');
  assert.ok(claudeJobStart >= 0 && createPrJobStart > claudeJobStart, 'expected both jobs in order');
  const claudeJobText = workflow.slice(claudeJobStart, createPrJobStart);
  const createPrJobText = workflow.slice(createPrJobStart);
  assert.doesNotMatch(claudeJobText, /CLAUDE_PR_TOKEN/,
    'the interactive Claude step must never receive the PR/branch-write PAT');
  assert.match(createPrJobText, /CLAUDE_PR_TOKEN/,
    'the trusted post-Claude job should be the only consumer of the PAT');
});

const assert = require('node:assert/strict');
const test = require('node:test');

const { classifyPath } = require('../risk_classify_paths.js');

const cases = [
  ['.github/workflows/deploy-auto.yml', 'high'],
  ['.github/workflows/deploy-gateway.yml', 'high'],
  ['.github/workflows/ci.yml', 'medium'],
  ['.github/workflows/pipeline-health.yml', 'medium'],
  ['.github/ISSUE_TEMPLATE/codex-dispatch.md', 'low'],
  ['.github/dependabot.yml', 'low'],
  ['.github/automerge-enabled', 'low'],
  ['scripts/automerge_gate.py', 'low'],
  ['AGENTS.md', 'low'],
  ['CLAUDE.md', 'low'],
  ['docs/83-project-continuity.md', 'low'],
  ['backend/tests/guards/test_x.py', 'low'],
  ['backend/app/providers/gateway/xray_file.py', 'medium'],
  ['infrastructure/alembic/versions/0024_x.py', 'migration'],
  ['deploy/lib/70_verify.sh', 'high'],
  ['backend/app/core/security.py', 'high'],
  ['Makefile', 'high'],
  ['some/brand/new/path.py', 'high'],
];

for (const [path, expected] of cases) {
  test(`${path} -> ${expected}`, () => {
    assert.equal(classifyPath(path), expected);
  });
}

const high = [
  path => path.startsWith('.github/workflows/deploy-'),
];

const low = [
  path => path.startsWith('.github/ISSUE_TEMPLATE/'),
  path => path.startsWith('.github/'),
  path => path.startsWith('frontend/'),
  path => path.startsWith('docs/'),
  path => path.startsWith('scripts/'),
  path => path === 'AGENTS.md',
  path => path === 'CLAUDE.md',
  path => path === 'README.md',
  path => path === 'ARCHITECTURE.md',
  path => path.startsWith('backend/app/api/'),
  path => path.startsWith('backend/app/domain/'),
  path => path.startsWith('backend/app/schemas/'),
  path => path.startsWith('backend/tests/'),
];

const medium = [
  path => path.startsWith('.github/workflows/'),
  path => path.startsWith('backend/app/providers/gateway/'),
  path => path.startsWith('backend/app/providers/forwarder/'),
  path => path.startsWith('backend/app/providers/egress/'),
  path => path.startsWith('ops/gateway/'),
  path => path.startsWith('ops/forwarder/'),
  path => path.startsWith('infrastructure/marzban/'),
  path => path.startsWith('infrastructure/compose/'),
];

const isMigration = path => path.startsWith('infrastructure/alembic/versions/');

const classifyPath = path => {
  if (isMigration(path)) return 'migration';
  if (high.some(match => match(path))) return 'high';
  if (medium.some(match => match(path))) return 'medium';
  if (low.some(match => match(path))) return 'low';
  return 'high';
};

module.exports = { classifyPath };

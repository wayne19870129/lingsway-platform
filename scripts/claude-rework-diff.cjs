// Captures the interactive Claude step's local, unpushed working-tree
// changes as a small JSON manifest (path + base64 content per changed file)
// for hand-off to the trusted post-Claude writer job. This module is pure
// parsing/building logic and is unit tested directly; the CLI entry point at
// the bottom (git/filesystem access) is thin glue exercised only by the
// workflow itself, matching the existing scripts/claude-create-pr.cjs split.
// The manifest travels between jobs as a single base64 GitHub Actions job
// output (no artifact upload/download action to pin/trust), so it must stay
// well under GitHub's per-output size limit; fail closed rather than truncate.
const MAX_TOTAL_CONTENT_BYTES = 300 * 1024;

function parseNameStatus(text) {
  const entries = [];
  for (const rawLine of (text || '').split('\n')) {
    const line = rawLine.trim();
    if (!line) continue;
    const [status, ...rest] = line.split('\t');
    const path = rest[rest.length - 1];
    if (!path) continue;
    const code = status[0];
    if (code === 'D') entries.push({ action: 'delete', path });
    else if (code === 'A' || code === 'M' || code === 'T' || code === 'C') entries.push({ action: 'upsert', path });
    // Renames/copies are expected to arrive pre-split as delete+upsert
    // because the capturing CLI runs `git diff --no-renames`; any other
    // status code is intentionally ignored rather than guessed at.
  }
  return entries;
}

function buildManifest({ entries, readFileBase64, commitMessage }) {
  let totalBytes = 0;
  const changes = [];
  for (const entry of entries) {
    if (!entry || typeof entry.path !== 'string' || !entry.path || entry.path.includes('..')) {
      throw new Error(`Refusing to include unsafe path in manifest: ${JSON.stringify(entry)}`);
    }
    if (entry.action === 'delete') {
      changes.push({ action: 'delete', path: entry.path });
      continue;
    }
    const content = readFileBase64(entry.path);
    totalBytes += content.length;
    if (totalBytes > MAX_TOTAL_CONTENT_BYTES) {
      throw new Error('Refusing to build manifest: total changed-file content exceeds the safety limit');
    }
    changes.push({ action: 'upsert', path: entry.path, content });
  }
  return { changes, commitMessage: commitMessage || null };
}

module.exports = { parseNameStatus, buildManifest, MAX_TOTAL_CONTENT_BYTES };

if (require.main === module) {
  const fs = require('node:fs');
  const { execFileSync } = require('node:child_process');
  const [baseSha, outputPath] = process.argv.slice(2);
  if (!baseSha || !outputPath) {
    console.error('Usage: node claude-rework-diff.cjs <baseSha> <outputPath>');
    process.exit(2);
  }
  const nameStatus = execFileSync('git', ['diff', '--name-status', '--no-renames', baseSha, '--'], { encoding: 'utf8' });
  const entries = parseNameStatus(nameStatus);
  const commitMessage = execFileSync('git', ['log', `${baseSha}..HEAD`, '--format=%B'], { encoding: 'utf8' }).trim();
  const manifest = buildManifest({
    entries,
    readFileBase64: (path) => fs.readFileSync(path).toString('base64'),
    commitMessage: commitMessage || null,
  });
  fs.writeFileSync(outputPath, JSON.stringify(manifest));
  console.log(`Wrote rework manifest with ${manifest.changes.length} change(s) to ${outputPath}`);
}

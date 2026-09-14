// Runs only in the trusted post-Claude job, authenticated with
// CLAUDE_PR_TOKEN, on a separate runner from the interactive Claude step.
// It never executes repository-controlled application/test code: every
// operation below is a GitHub Git Data API call (blob/tree/commit/ref)
// against a manifest of {path, content} pairs produced earlier by
// scripts/claude-rework-diff.cjs, never a `git clone`/`npm ci`/`pytest`/etc.
// against the target branch. See docs/82-tasks/TASK-T26-trusted-pr-rework-writer.md.
//
// Automation-owned branches are only ever created by the Issue-first flow in
// scripts/claude-create-pr.cjs, which names them `claude/issue-<n>-<token>`.
// A branch that does not match this exact naming policy is never written to,
// even if it happens to live in this repository and reference the same PR.
const AUTOMATION_BRANCH_POLICY = /^claude\/issue-[0-9]+-[0-9A-Za-z-]+$/;

module.exports = async function applyPrRework({ github, context, core, manifest, prNumber, branch, baseSha }) {
  const { owner, repo } = context.repo;

  if (!prNumber || !branch || !baseSha) {
    throw new Error('Malformed transfer: missing prNumber/branch/baseSha from the interactive job');
  }
  if (!manifest || !Array.isArray(manifest.changes)) {
    throw new Error('Malformed transfer: manifest is missing or has no changes array');
  }
  if (manifest.changes.length === 0) {
    core.info('Manifest carries no changes; nothing to apply');
    return;
  }
  for (const change of manifest.changes) {
    if (!change || typeof change.path !== 'string' || !change.path || change.path.includes('..') || change.path.startsWith('/')) {
      throw new Error(`Malformed transfer: unsafe or invalid path in manifest (${JSON.stringify(change && change.path)})`);
    }
    if (change.action !== 'upsert' && change.action !== 'delete') {
      throw new Error(`Malformed transfer: unknown change action ${JSON.stringify(change.action)}`);
    }
    if (change.action === 'upsert' && typeof change.content !== 'string') {
      throw new Error(`Malformed transfer: upsert entry for ${change.path} is missing content`);
    }
  }

  // Fail closed before touching the API at all if the branch name itself
  // does not match this automation's own naming policy - covers both a
  // fork PR's differently-named head and a same-repo human-owned branch.
  if (!AUTOMATION_BRANCH_POLICY.test(branch)) {
    throw new Error(`Refusing to write: '${branch}' does not match the automation-owned branch naming policy`);
  }

  const { data: pr } = await github.rest.pulls.get({ owner, repo, pull_number: prNumber });

  if (pr.state !== 'open') {
    throw new Error(`Refusing to write: PR #${prNumber} is not open (state=${pr.state})`);
  }
  if ((pr.head.repo?.full_name || '').toLowerCase() !== `${owner}/${repo}`.toLowerCase()) {
    throw new Error(`Refusing to write: PR #${prNumber} head is not this repository (fork or missing head repo)`);
  }
  if (pr.head.ref !== branch) {
    throw new Error(`Refusing to write: PR #${prNumber} head ref '${pr.head.ref}' no longer matches expected branch '${branch}'`);
  }
  if (pr.head.sha !== baseSha) {
    throw new Error(`Stale transfer: PR #${prNumber} head moved from ${baseSha} to ${pr.head.sha}; refusing to overwrite concurrent work`);
  }

  const { data: baseCommit } = await github.rest.git.getCommit({ owner, repo, commit_sha: pr.head.sha });

  const tree = manifest.changes.map((change) => change.action === 'delete'
    ? { path: change.path, mode: '100644', type: 'blob', sha: null }
    : { path: change.path, mode: '100644', type: 'blob' });

  // Blobs are created before the tree so a mid-way API failure here throws
  // and fails closed without ever calling createTree/createCommit/updateRef.
  const blobShaByPath = new Map();
  for (const change of manifest.changes) {
    if (change.action !== 'upsert') continue;
    const { data: blob } = await github.rest.git.createBlob({ owner, repo, content: change.content, encoding: 'base64' });
    blobShaByPath.set(change.path, blob.sha);
  }
  for (const entry of tree) {
    if (entry.sha === null) continue; // deletion
    entry.sha = blobShaByPath.get(entry.path);
  }

  const { data: newTree } = await github.rest.git.createTree({
    owner, repo, base_tree: baseCommit.tree.sha, tree,
  });

  const message = manifest.commitMessage && manifest.commitMessage.trim()
    ? manifest.commitMessage.trim()
    : `Rework PR #${prNumber} via trusted writer`;
  const { data: newCommit } = await github.rest.git.createCommit({
    owner, repo, message, tree: newTree.sha, parents: [pr.head.sha],
  });

  try {
    // force:false makes this a fast-forward-only update: if the ref moved
    // between the pulls.get check above and this call, GitHub rejects it
    // rather than letting a race silently overwrite concurrent work.
    await github.rest.git.updateRef({ owner, repo, ref: `heads/${branch}`, sha: newCommit.sha, force: false });
  } catch (error) {
    if (error.status === 422 || error.status === 409) {
      throw new Error(`Stale transfer: '${branch}' moved concurrently before the trusted write landed (status ${error.status})`);
    }
    throw error;
  }

  core.info(`Trusted writer pushed ${newCommit.sha} onto ${branch} for PR #${prNumber}; normal pull_request checks will run automatically`);
};

module.exports.AUTOMATION_BRANCH_POLICY = AUTOMATION_BRANCH_POLICY;

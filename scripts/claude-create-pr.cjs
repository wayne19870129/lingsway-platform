// Runs only from the event's trusted default-branch SHA on a separate runner.
module.exports = async function createClaudePR({ github, context, core, branch }) {
  const { owner, repo } = context.repo;
  const issue = context.payload.issue;
  if (!issue || issue.pull_request || !branch) return;
  const prefix = `claude/issue-${issue.number}-`;
  if (!branch.startsWith(prefix) || !/^[A-Za-z0-9/_-]+$/.test(branch)) {
    throw new Error('Unexpected Claude branch for this Issue');
  }
  const sameIssue = (pr) => {
    const ownBranch = pr.head.repo?.full_name?.toLowerCase() === `${owner}/${repo}`.toLowerCase();
    return (ownBranch && pr.head.ref.startsWith(prefix)) ||
      new RegExp(`(?:^|[^A-Za-z0-9_/])#${issue.number}(?![0-9])`).test(pr.body || '') ||
      (pr.body || '').split(issue.html_url).slice(1).some((tail) => !/^[0-9]/.test(tail));
  };
  // Include closed/merged head PRs: reruns must not reopen finished work.
  const headPRs = await github.paginate(github.rest.pulls.list, {
    owner, repo, head: `${owner}:${branch}`, state: 'all', per_page: 100,
  });
  if (headPRs.length) return core.info(`Head already has PR #${headPRs[0].number}`);

  let diff;
  try {
    ({ data: diff } = await github.rest.repos.compareCommitsWithBasehead({
      owner, repo, basehead: `main...${branch}`, per_page: 100,
    }));
  } catch (error) {
    // Question-only tag runs can report a local branch that was never pushed.
    if (error.status === 404) return core.info('No remotely comparable branch; no PR created');
    throw error;
  }
  if (diff.ahead_by < 1 || !diff.files?.length) {
    return core.info('No changes ahead of main; no PR created');
  }

  const openPRs = await github.paginate(github.rest.pulls.list, {
    owner, repo, state: 'open', per_page: 100,
  });
  const existing = openPRs.find(sameIssue);
  // Sanitized, data-only summary: no shell evaluation of Issue/commit text.
  const summary = [
    'Commits from this Issue run:',
    ...(diff.commits || []).slice(-20).map((c) =>
      `- ${c.commit.message.split('\n')[0].replace(/[\r\n]/g, ' ').slice(0, 200)}`),
    '', 'Files touched:',
    ...diff.files.slice(0, 50).map((f) => `- \`${f.filename}\``),
  ].join('\n');

  if (existing) {
    if (existing.head.ref === branch) {
      return core.info(`PR #${existing.number} already tracks ${branch}`);
    }
    // Keep one PR per Issue without granting this token branch-write access.
    // A durable, idempotent pointer makes the later branch visible for a
    // maintainer to reconcile; this helper never merges or deletes branches.
    const marker = `<!-- claude-pending-branch:${branch} -->`;
    if ((existing.body || '').includes(marker)) {
      return core.info(`PR #${existing.number} already references pending branch ${branch}`);
    }
    await github.rest.pulls.update({
      owner, repo, pull_number: existing.number,
      body: [existing.body || '', marker,
        `Additional Issue #${issue.number} work was pushed to \`${branch}\`. ` +
        'To preserve one Issue / one PR and least-privilege Contents read access, ' +
        'automation did not create a second PR or modify this PR branch. ' +
        'A maintainer must reconcile the referenced branch manually.',
        summary].join('\n\n'),
    });
    return core.info(`Recorded pending branch ${branch} on existing PR #${existing.number}`);
  }

  const runURL = `${context.serverUrl}/${owner}/${repo}/actions/runs/${context.runId}`;
  // Fixed template and API JSON fields: no shell evaluation of Issue/commit text.
  await github.rest.pulls.create({
    owner, repo, base: 'main', head: branch,
    title: `Issue #${issue.number}: ${issue.title}`.replace(/[\r\n]/g, ' ').slice(0, 240),
    body: [
      '## Summary', `Implementation for ${issue.html_url}.`, summary,
      'See the Claude response on the Issue for the full implementation rationale.',
      '## Verification', `Claude execution: ${runURL}.`,
      'See the Claude response for the exact commands run and their results; a successful run alone does not prove tests passed.',
      'Normal PR CI/Security/Risk checks must pass before human review and merge.',
      '## Existing-customer impact', 'Review the diff and Issue acceptance criteria before merging.',
      '## Rollback plan', 'If merged, revert through a separately reviewed PR.',
      '## Risk classification', 'Use the normal Risk classification check and human review.',
      'Human merge is mandatory. This PR does not automatically close the Issue.',
    ].join('\n\n'),
  });
  core.info('Created PR; normal pull_request workflows will evaluate it');
};

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
  const openPRs = await github.paginate(github.rest.pulls.list, {
    owner, repo, state: 'open', per_page: 100,
  });
  const existing = openPRs.find(sameIssue);
  if (existing) return core.info(`Issue already has PR #${existing.number}`);

  let diff;
  try {
    ({ data: diff } = await github.rest.repos.compareCommitsWithBasehead({
      owner, repo, basehead: `main...${branch}`, per_page: 1,
    }));
  } catch (error) {
    // Question-only tag runs can report a local branch that was never pushed.
    if (error.status === 404) return core.info('No remotely comparable branch; no PR created');
    throw error;
  }
  if (diff.ahead_by < 1 || !diff.files?.length) {
    return core.info('No changes ahead of main; no PR created');
  }
  const runURL = `${context.serverUrl}/${owner}/${repo}/actions/runs/${context.runId}`;
  // Fixed template and API JSON fields: no shell evaluation of Issue/commit text.
  await github.rest.pulls.create({
    owner, repo, base: 'main', head: branch,
    title: `Issue #${issue.number}: ${issue.title}`.replace(/[\r\n]/g, ' ').slice(0, 240),
    body: [
      '## Summary', `Implementation for ${issue.html_url}.`,
      'See the diff and Claude response on the Issue for the implementation details.',
      '## Verification', `Claude execution: ${runURL}.`,
      'See the Claude response for commands and results; a successful run alone does not prove tests passed.',
      'Normal PR CI/Security/Risk checks must pass before human review and merge.',
      '## Existing-customer impact', 'Review the diff and Issue acceptance criteria before merging.',
      '## Rollback plan', 'If merged, revert through a separately reviewed PR.',
      '## Risk classification', 'Use the normal Risk classification check and human review.',
      'Human merge is mandatory. This PR does not automatically close the Issue.',
    ].join('\n\n'),
  });
  core.info('Created PR; normal pull_request workflows will evaluate it');
};

// Runs only from the event's trusted default-branch SHA on a separate runner.
module.exports = async function createClaudePR({ github, context, core, branch }) {
  const { owner, repo } = context.repo;
  const issue = context.payload.issue;
  if (!issue || issue.pull_request || !branch) return;
  const prefix = `claude/issue-${issue.number}-`;
  if (!branch.startsWith(prefix) || !/^[A-Za-z0-9/_-]+$/.test(branch)) {
    throw new Error('Unexpected Claude branch for this Issue');
  }
  const isOwnHead = (pr) => pr.head.repo?.full_name?.toLowerCase() === `${owner}/${repo}`.toLowerCase();
  const sameIssue = (pr) => {
    return (isOwnHead(pr) && pr.head.ref.startsWith(prefix)) ||
      new RegExp(`(?:^|[^A-Za-z0-9_/])#${issue.number}(?![0-9])`).test(pr.body || '') ||
      (pr.body || '').split(issue.html_url).slice(1).some((tail) => !/^[0-9]/.test(tail));
  };
  // Only a same-repo branch this automation itself created (matches the
  // Issue's own naming prefix) is safe to write to. A PR merely referencing
  // the Issue in prose may be a human's own branch; never force-write there
  // even though the newly approved PAT scope would technically allow it.
  const isClaudeOwnedHead = (pr) => isOwnHead(pr) && pr.head.ref.startsWith(prefix);
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
  // Trustworthy full-vs-partial signal: only an exact, standalone line in a
  // real commit on this Issue's own branch counts, never free-text/prose.
  // Never emit a literal Closes/Fixes/Resolves token in the negative case
  // (see docs/83-project-continuity.md's #77/#76 incident) - the two
  // branches below use disjoint wording, not a negated keyword.
  const closesMarker = new RegExp(`^(?:Closes|Fixes|Resolves)\\s+#${issue.number}$`, 'i');
  const fullyResolved = (diff.commits || []).some((c) =>
    c.commit.message.split('\n').some((line) => closesMarker.test(line.trim())));
  const closureNote = fullyResolved
    ? `Closes #${issue.number}`
    : `Issue #${issue.number} stays open; the linked commit did not mark it fully resolved.`;
  // Raw commit subjects are quoted verbatim below for review context, but a
  // subject can itself contain an accidental "Closes #N" for some other
  // Issue; GitHub's keyword parser does not care about surrounding prose
  // (the same #77/#76 failure mode). Defuse any such token in quoted text
  // with a zero-width space so only the trusted closureNote line above can
  // ever act as a real closing reference.
  const zeroWidthSpace = String.fromCharCode(8203);
  const defuseKeywords = (s) =>
    s.replace(/\b(closes?|closed|fix(?:es|ed)?|resolves?|resolved)(\s+)#(\d+)/gi,
      (_, kw, sp, num) => `${kw}${sp}#${zeroWidthSpace}${num}`);
  // Sanitized, data-only summary: no shell evaluation of Issue/commit text.
  const summary = [
    'Commits from this Issue run:',
    ...(diff.commits || []).slice(-20).map((c) =>
      `- ${defuseKeywords(c.commit.message.split('\n')[0].replace(/[\r\n]/g, ' ').slice(0, 200))}`),
    '', 'Files touched:',
    ...diff.files.slice(0, 50).map((f) => `- \`${f.filename}\``),
  ].join('\n');

  if (existing) {
    if (existing.head.ref === branch) {
      return core.info(`PR #${existing.number} already tracks ${branch}`);
    }
    // Same-Issue follow-up work must reach the existing PR's own head/CI,
    // not just be recorded in prose. The maintainer-approved narrow
    // Contents:write grant (confined to this trusted post-Claude job; never
    // exposed to the Claude step or repository-controlled code) allows a
    // real branch merge, but only onto a branch this automation itself
    // created for this Issue. A same-Issue PR matched only by body text
    // (a human's own branch) is never written to — that would be force-
    // writing into an unexpected/unsafe branch relationship.
    const claudeOwnedHead = isClaudeOwnedHead(existing);
    if (claudeOwnedHead) {
      try {
        await github.rest.repos.merge({
          owner, repo, base: existing.head.ref, head: branch,
          commit_message: `Merge ${branch} into ${existing.head.ref} for Issue #${issue.number}`,
        });
        return core.info(`Merged ${branch} into existing PR #${existing.number}'s head; its CI will re-run`);
      } catch (error) {
        if (error.status !== 409 && error.status !== 404) throw error;
        core.warning(`Could not merge ${branch} into PR #${existing.number}'s head (${error.status}); recording a pointer instead`);
      }
    }
    // Fallback: a durable, idempotent pointer makes the later branch visible
    // for a maintainer to reconcile manually. Reached either because the
    // existing PR's head is not an automation-owned branch (fork or a
    // human/manual PR), or because the merge above hit a real conflict.
    const marker = `<!-- claude-pending-branch:${branch} -->`;
    if ((existing.body || '').includes(marker)) {
      return core.info(`PR #${existing.number} already references pending branch ${branch}`);
    }
    await github.rest.pulls.update({
      owner, repo, pull_number: existing.number,
      body: [existing.body || '', marker,
        claudeOwnedHead
          ? `Additional Issue #${issue.number} work on \`${branch}\` could not be merged into this PR's head automatically (merge conflict or missing branch). A maintainer must merge or rebase it into this PR manually.`
          : `Additional Issue #${issue.number} work was pushed to \`${branch}\`. This PR's head is not a branch this automation created, so it was not written to automatically. A maintainer must reconcile the referenced branch manually.`,
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
      'Human merge is mandatory.', closureNote,
    ].join('\n\n'),
  });
  core.info('Created PR; normal pull_request workflows will evaluate it');
};

# Working agreement: how we build here

This is the standing way of working for this repo. Every agent and every Claude session
follows it. It is adapted from the owner's house standard for all their repos. The owner is
`thatskiff33`.

## The shape of the work

**Epic, then waves, then issues, then one PR per issue.**

- The work is tracked in an **Epic** issue (label `epic`) that groups issues into
  **waves** run in order. Independent issues within a wave can run in parallel; anything
  touching the same files is done one after another.
- Each issue is scoped to become **one clean pull request**.
- Every issue is written **in plain English**, with what it delivers, a `## Decisions`
  section (options, each with a **Recommended** default) wherever a real choice exists,
  and a "Done when" check. Issues waiting on a choice carry the `needs-decision` label.

## The owner's role

The owner does not review PRs (owner decision, 26 Oct 2026). Work runs autonomously.

- **Decisions:** when the owner is in a Claude session, real choices are asked there as
  pop-up questions, not by ticking boxes on GitHub. Otherwise the **Recommended** option is
  used. Either way the session ticks the chosen option on the issue, records it in a
  comment, and removes `needs-decision`.
- **Merging:** no approval comment is needed. A PR merges as soon as it is ready (below).

## Who builds: Sekreton

Issues are built by [Sekreton](https://sekreton.com) tasks in the project
`nz-portfolio-digest` (task prefix `NZP`). One task per issue; its brief carries the
issue's scope and recorded decisions, and `depends_on` keeps waves in order.

- The task sandbox runs [`.sekreton/config.toml`](../.sekreton/config.toml): it installs the
  app with dev tools and runs `ruff` and `pytest`. Database tests skip there (no Postgres);
  GitHub CI runs them against a real Postgres, and must be green before merge.
- Sekreton does the ideas, plans, implementation, review and fixes (owner decision, 27 Oct
  2026). Other agents direct and unblock it and do the GitHub steps, because Sekreton's
  sandboxes can't write to GitHub. [AGENTS.md](../AGENTS.md) sets out exactly how.
- Every task has auto-merge on. The directing agent merges a task in diff review as soon as
  its gates pass and Sekreton's review has finished (this opens its PR), then merges the PR
  when GitHub CI is green.

## The build loop

For Sekreton tasks, Sekreton runs steps 2 to 5 itself (build, its own independent reviews,
fixes, residual risks), and the directing agent handles steps 6 and 7 as set out in
[AGENTS.md](../AGENTS.md). The steps below are the standard any builder follows.

1. **Claim** the issue (comment on it); branch `feat/<n>-<slug>`; open a **draft** PR
   early whose body ends with `Closes #<n>`.
2. **Build** to the chosen decisions. If a decision was never made, use the
   **Recommended** option and say so in the PR.
3. **Review** with **several independent review sub-agents at once**, chosen to fit the
   issue: correctness and edge cases, security (secrets, injection, exposure), financial
   accuracy (money maths, rounding, currency), privacy of personal financial data,
   operability (deploy, backups, failure modes), house style, and at least one
   **adversarial** "try to break this" pass.
4. **Fix** every real finding; re-review until clean.
5. **Resolve residual risks in the same PR.** Open a new `needs-decision` issue only for
   something that genuinely needs the owner's choice or a fresh plan, never a silent to-do.
6. **Ready:** run the checks locally (`ruff check .`, `ruff format --check .`, `pytest`
   against a throwaway Postgres) and in CI, then post a ✅ **Ready to merge** comment in
   plain English: what was built, which reviews ran, what they found, how each was fixed,
   test results, and any carry-forwards.
7. **Merge** straight after the ready comment: squash-merge, tick the Epic checklist,
   delete the branch, then move on to the next issue.

## Firm rules

- **Never merge a PR that is unreviewed, has unfixed findings, or is not green** on its
  exact head.
- **No secrets in the repo**: `.env`, app passwords, API tokens and Akahu tokens live only
  on the server. Error messages and logs never print them.
- **No personal financial data in the repo**: broker exports, statements and database
  dumps stay out of git (see `.gitignore`). Test fixtures use made-up or anonymised data.
- **Nothing is exposed to the public internet.** The dashboard is reachable over
  Tailscale only.
- **Money is exact**: `Decimal` and `Numeric`, never floats.
- **No Sharesight integration** (owner decision, 26 Oct 2026): no scraping, API or export
  import.
- **House style**: NZ English, plain and warm, no em dashes.
- **Escalate by commenting, never by guessing** on anything irreversible or
  security-sensitive.

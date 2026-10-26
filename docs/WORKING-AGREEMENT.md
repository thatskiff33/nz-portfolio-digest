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

## The owner's role (two touch-points per issue)

1. **Before work starts:** decisions are asked **in the Claude session as pop-up
   questions**, not by ticking boxes on GitHub. The session then ticks the chosen options
   on the issue, records them in a comment, and removes `needs-decision`.
2. **Before each merge:** when a PR is marked ✅ *Ready to merge*, read the plain-English
   summary and, if happy, comment exactly **`Approved and ok to merge`** on that PR.

## Who builds: Sekreton

Issues are built by [Sekreton](https://sekreton.com) tasks in the project
`nz-portfolio-digest` (task prefix `NZP`). One task per issue; its brief carries the
issue's scope and recorded decisions, and `depends_on` keeps waves in order.

- The task sandbox runs [`.sekreton/config.toml`](../.sekreton/config.toml): it installs the
  app with dev tools and runs `ruff` and `pytest`. Database tests skip there (no Postgres);
  GitHub CI runs them against a real Postgres, and must be green before merge.
- Sekreton's sandboxes can't comment on GitHub. A Claude session (the orchestrator) does
  the claim, review, *Ready to merge* comment and merge steps below.
- Sekreton tasks never auto-merge. The owner's approval phrase still gates every merge.

## The build loop

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
7. **Merge** only on the owner's exact comment `Approved and ok to merge`: squash-merge,
   tick the Epic checklist, delete the branch. Keep building the next issue while waiting.

## Firm rules

- **Never merge without the exact approval phrase**, per PR, never assumed.
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

# Agents: how to work on this repo

This file is for every agent working here: Claude Code sessions, Codex, the WSL orchestrator
and Sekreton's own task agents. Read it first, then
[docs/WORKING-AGREEMENT.md](docs/WORKING-AGREEMENT.md) for the house rules.

## Sekreton does the work

[Sekreton](https://sekreton.com) builds this repo. The project is `nz-portfolio-digest` in
the team "Tokoroa Alpine Club Incorporated", and its tasks are prefixed `NZP`.

- **Sekreton does:** ideas, plans, implementation, review, fixes and conflict resolution,
  one task per issue.
- **Every other agent directs and unblocks.** You keep Sekreton moving, and you merge.
- **The owner does not review or approve anything.** Nothing should ever be waiting on them
  (owner decisions, 26 and 27 Oct 2026).

Sekreton is reached through its MCP server (`https://sekreton.com/mcp`, tools named
`mcp__sekreton__*`). Its task sandboxes cannot write to GitHub, which is why an outside
agent does the GitHub steps.

## Your job as the directing agent

1. **Give Sekreton the work.**
   - For each issue in the epic, create a task with `create_task`.
   - The brief carries the issue's scope and recorded decisions. Use `depends_on` to keep
     the waves in order.
   - Run `search_backlog` first so you don't create duplicates.
   - Use `one_shot` for small, fully specified work and `interactive` to let Sekreton plan
     bigger work.
2. **Turn on auto-merge for every task** (`auto_merge: true` on create, or `set_auto_merge`
   before it reaches diff review).
   - The task then merges by itself once its verification is green, Sekreton's review has
     finished, nothing conflicts and no comments are waiting.
3. **Approve plans.** When a task is waiting on a person at planning, choose the
   **Recommended** option for each choice, record anything significant on the issue, and
   `approve_plan`.
4. **Never leave a task in diff review.**
   - **Ready:** if its gates passed and Sekreton's review has finished, `merge_task`
     straight away.
   - **Not ready:** if gates failed, review findings are still unfixed, or a quick skim of
     `get_diff` shows something plainly wrong (a secret, real personal data, a broken
     contract with another issue), send it back with `request_changes` and a short message.
   - **Your own reviews:** don't layer your own review sub-agents on top of Sekreton's
     review.
5. **Hand problems back to Sekreton first.**
   - **Failed or stopped task:** `retry_task`.
   - **Merge conflict:** `retry_task`, which re-runs conflict resolution.
   - **Red CI on its PR:** `request_changes` with the log excerpt.
   - **Last resort:** fix it yourself on the PR branch only when Sekreton can't take the
     change.
6. **Land the PR Sekreton opens** (head branch `sekreton/.../nzp-<n>`).
   - Wait for CI to be green on the exact head.
   - Post a short plain-English **✅ Ready to merge** comment: what was built, Sekreton's
     review result, the CI result and any carry-forwards.
   - Make sure the body says `Closes #<n>`, then
     `gh pr merge <n> --squash --delete-branch`.
   - Tick the issue on the epic checklist and confirm the issue closed.
7. **Carry-forwards become Sekreton tasks** (with auto-merge on). Raise a `needs-decision`
   issue only for a real owner choice, give it a Recommended option, and use that option
   while the owner is away.
8. **Only the owner can fix some things**, such as Sekreton running out of credits. Note
   them once on the epic, then retry when they're fixed.

## Merging and privacy (public repo)

- **Sekreton's PRs:** merge without `--author-email`. GitHub credits the squash to the PR's
  author, sekreton[bot], at its no-reply address. It rejects an author email that isn't the
  merging account's own.
- **PRs you open yourself as `thatskiff33-agents`:** pass
  `--author-email 318094810+thatskiff33-agents@users.noreply.github.com`, so no private
  address is stamped on `main`.
- **After every merge:** check that `git log origin/main -1 --format=%ae` is a
  `users.noreply.github.com` address.
- **Never:** push to `main` directly, force-push, or commit secrets, `.env` files, broker
  exports or real portfolio data.

## Migrations when tasks run in parallel

Two tasks that each add an Alembic revision on the same parent will clash. Whichever merges
second must re-chain its revision onto `main`'s head. Check with `alembic heads`; there must
be exactly one. Ask Sekreton to do the re-chain through `request_changes` or `retry_task`.

## The always-on orchestrator

A headless Claude Code run on the owner's WSL host does all of the above every 10 minutes.
It uses a systemd user timer, with its instructions and state in `~/src/nzp-orchestrator/`.
An interactive session can take over by touching `~/src/nzp-orchestrator/state/INTERACTIVE`,
and the timer steps aside while that file is under 20 minutes old.

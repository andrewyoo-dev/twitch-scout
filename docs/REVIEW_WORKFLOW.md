# Claude implementation and Astra review

## Chosen setup

Claude Code handles implementation and fixes; Codex with Astra handles independent review and focused re-review.
The user selects Astra in Codex; these files do not change the active model.
Handoffs use local Markdown files with manual task initiation, without new services or automatic agent dispatch.
The active review lives in `reviews/review-feedback.md`, a local, gitignored file (a fresh clone will not have it); create the `reviews/` directory when starting a review.
This setup creates the workflow only; it does not establish that the application has been reviewed.

## Starting a review

Record the requested behavior, acceptance criteria, scope exclusions, and exact review target before assessing the code.
For committed changes, record both base and head commits rather than assuming a branch or reviewing only the latest commit.
For uncommitted changes, record HEAD, the time, the included paths, and whether staged, unstaged, and untracked files are included.
If the requested target cannot be inferred, ask the user to identify it.
Avoid simultaneous edits to the same files during review, and check for target changes before publishing findings.
If the code changes during review, identify which conclusions need rechecking.

Read the diff and enough surrounding code, callers, and tests to evaluate the behavior.
Use [TWITCH_SCOUT_HANDOFF.md](TWITCH_SCOUT_HANDOFF.md) for product intent and the current code and configuration for implementation evidence.
Durable design decisions are in [DECISIONS.md](DECISIONS.md).
Historical handoffs may contain superseded plans or old test counts; surface material conflicts instead of treating either as current proof.

## Astra review

Prioritize correctness, regressions, data integrity, security, resource bounds, and failure handling.
For this project, consider sampling windows and daylight saving time, duplicate collection and partial failures, SQLite/Turso behavior, API pagination and retries, ranking noise guards, and CLI input validation when affected by the change.
Do not turn unrelated style preferences or optional refactors into blocking defects.

Reproduce suspected bugs at the smallest level appropriate to their risk, following the coding-standards skill.
Use focused unit or integration checks where sufficient, and use end-to-end verification for user-facing or high-risk defects.
During a review-only request, do not change application code; add a narrowly scoped regression test only if needed to demonstrate a defect, and report any added files.
Use fixtures and disposable local databases for checks; live collection or production writes require task-specific authorization.
Do not read or reproduce credentials in reports.

Every actionable finding needs a stable ID, priority, exact location, trigger, expected and actual behavior, concrete impact, evidence, a proposed correction, and a verification method.
Evidence may be a reproduction or a precise code-path explanation; label which one was used.
Keep unverified suspicions in a separate section, with the evidence still needed.
Report no actionable findings when appropriate, and always state the scope and verification limits.

| Priority | Meaning |
| --- | --- |
| P0 | Confirmed critical issue requiring immediate attention, such as ongoing data loss or exposed credentials. |
| P1 | High-impact defect in a core or likely workflow. |
| P2 | Material defect under a specific supported condition. |
| P3 | Low-impact, actionable defect. |

## Claude response and fixes

Read the review target and check that each finding still applies to the current code.
Validate findings independently before accepting them.
For each item, record accepted, rejected, needs clarification, or deferred, with supporting reasoning.
Rejecting a finding requires evidence; deferral requires a reason and must remain visible as unresolved work.
For accepted bugs, reproduce the problem before the fix and verify the same behavior afterward.
Keep changes focused on the accepted findings and preserve unrelated user changes.
Record changed paths or commits, commands actually run, observed results, and remaining limitations.
Implementation marks an item fixed pending review, not independently verified.

## Verification and closing

Use the project's existing checks as appropriate to the code changed:

```text
ruff check .
ruff format --check .
mypy twitch_scout
python -m pytest -q
```

Run commands separately when the shell needs that to preserve each exit status.
Record failed, skipped, or blocked checks explicitly instead of copying historical success counts.
Documentation-only changes need link, consistency, and diff checks rather than the application test suite.

Astra rechecks accepted fixes and the code paths they affect for regressions.
Record the exact revision or working-tree scope rechecked, the evidence, and whether each finding is resolved or remains open.
If a new unrelated issue appears, track it separately instead of silently expanding the current review.
Close the cycle when accepted findings are verified and unresolved decisions or deferrals are explicitly recorded.
If no independent re-review occurred, retain that limitation.

Before beginning a different review scope, preserve the completed report under `reviews/` with a descriptive date-and-topic filename, then reset the active report's fields.
Do not erase the old findings or fabricate a completed review to initialize a new one.

## Starting tasks

For review, identify the change range or working-tree files and the intended behavior, and ask Astra to use this workflow and fill the active report.
For implementation, ask Claude Code to evaluate the active report, fix valid findings, and record decisions and verification for each item.
For re-review, identify the fixes and ask Astra to verify the previously reported issues and affected behavior.
These are separate user-initiated tasks; no message is automatically sent to either tool.

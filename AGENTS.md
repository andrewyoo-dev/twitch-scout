# Project agent rules

- Follow the user's global rules and the `coding-standards` skill for code changes or reviews.
- For the Claude implementation and Astra review workflow, read [docs/REVIEW_WORKFLOW.md](docs/REVIEW_WORKFLOW.md).
- Keep review findings and implementation responses in the local `reviews/` directory, only when it exists (it is gitignored; a fresh clone will not have it). Confirmed fixes belong in commits and tests, durable decisions in [docs/DECISIONS.md](docs/DECISIONS.md).
- A one-off handoff or AI prompt stays local or in `docs/`; when the work is done, move only the durable decision into `docs/DECISIONS.md` and delete the rest rather than leaving it at the repo root.
- Review requests default to inspection and verification; implementation requires a request to fix the findings.
- Treat historical handoffs as context, not proof of current behavior or test results.
- Never include credentials, environment-file contents, or production data in review documents.
- Do not use em-dashes or add agent co-author trailers to commits.
- In long Markdown documents, put each sentence on its own physical line.

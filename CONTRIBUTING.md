# Contributing

Start from an updated `main` and create a short-lived branch for one change:

```bash
git switch main
git pull --ff-only origin main
git switch -c feature/describe-the-change
```

Use `feature/`, `fix/`, `docs/` or `chore/` according to scope. Make focused commits
with imperative messages such as `fix: reject invalid embedding vectors`. Keep
code and its regression coverage together during ongoing development.

Install dependencies as described in the README. Run `python scripts/check.py`
before opening a pull request. Container changes additionally require
`docker compose config --quiet` and `docker compose build` on a Docker host.
Include a reproducible example and report any checks you could not execute.

Never commit local configuration, credentials, databases, model files or caches.
Use fictional fixtures and `.env.example` for configuration examples. Review
`git diff --cached` before committing.

Merge reviewed, passing work into `main` with a merge commit, then delete the
completed branch. See [Git workflow](docs/git-workflow.md) for integration and
publication details.

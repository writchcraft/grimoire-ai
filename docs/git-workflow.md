# Git workflow

`main` is the integration branch for completed work. Start each change from an
updated `main`, use focused commits and open a pull request. Recommended names:

| Branch | Scope |
| --- | --- |
| `feature/native-runtime` | Local generation and embedding lifecycle |
| `feature/rag-improvements` | Ingestion, retrieval and evidence |
| `feature/model-routing` | Independent generation/embedding selection |
| `feature/log-analysis` | Log evidence and incident reports |
| `docs/architecture` | Design decisions and operational documentation |
| `fix/<issue>` | A specific defect with regression coverage |

Use these names for active work only. Delete a branch once merged; create it
again from current `main` when a new change needs it. Prefixes are conventions,
not a reason to keep idle branches on the remote.

Before merging, review the complete diff and run `python scripts/check.py`.
Container changes require Compose config/build checks. Keep known real-model
acceptance limitations visible in the validation record. Merge completed work
with `git merge --no-ff <branch>` so integration boundaries remain visible.
Do not force-push shared `main` or fabricate commit dates.

Publish with `git push origin main`. Push a feature branch only when it is useful
for collaboration or review. If another contributor updates the remote first,
fetch and integrate their work; never overwrite it to make a push succeed.

The initial history imports an already implemented application into focused
module, test and documentation commits at the time of repository organization.
These commits record the import, not a claim that implementation occurred over
an earlier period. Feature merge boundaries describe the imported subsystems.
Intermediate import commits can reference modules introduced later in the same
integration batch; the completed application is validated at the final tip.

GitHub branch protection is an administrator setting: require pull requests and
passing backend/frontend/container checks before merging when those checks are
available. Local conventions alone cannot enforce remote protection.

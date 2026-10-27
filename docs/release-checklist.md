# Release checklist

- Run `python scripts/check.py` against the exact revision to publish.
- Review `git status --short`, `git diff --check` and the staged/published diff.
- Check that only `.env.example` is tracked and no weights, local database,
  private keys, caches or generated output appear in the tracked file list.
- On a Docker host, run `docker compose config --quiet` and build both images.
- Supply compatible local generation and embedding GGUFs explicitly. Verify
  startup, chat, ingestion, retrieval with sources, logs and incident export.
- Restart and confirm persistence; delete a document and verify both searches.
- Exercise degraded startup without weights and confirm useful offline workflows.
- Follow the acceptance procedure in `validation.md`, including backup/restore.
- Update validation notes with actual outcomes, environment and remaining limits.
- Merge completed work to `main`, push it and inspect CI results on GitHub.
- Tag a release only when its acceptance criteria are met. Commit counts and
  passing mocked tests alone do not establish real-model or container readiness.

# claudino

Slack relay that runs headless Claude Code per Slack thread. Start at [README.md](README.md): architecture, file map, configuration.

- Tests and lint: `scripts/test.sh` (ruff, then the suite). Slack and Claude are faked; tests must never need network, tokens or a real server.
- CI and deploys run on Depot ([`.depot/workflows/ci-cd.yml`](.depot/workflows/ci-cd.yml)), not GitHub Actions: read status with `gh pr checks` or the commit's check-runs (`gh api repos/{owner}/{repo}/commits/<sha>/check-runs`); `gh run` shows nothing current.
- Every setting is an env var read in [`config.py`](config.py). Add new ones there, in [`.env.example`](.env.example) and in the README table. Keep deployment-specific values (IDs, hosts, URLs) in config, never in code.
- [`claudino_instructions.md`](claudino_instructions.md) is the shared system prompt. Team-specific instructions belong in the server's `~/.config/claude-relay/instructions.md`.
- Merges to `main` deploy to production: keep `deploy/deployer.py` and `scripts/deploy.sh` rollback-safe.

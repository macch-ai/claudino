# claudino

Slack relay that runs headless Claude Code per Slack thread. Start at [README.md](README.md): architecture, file map, configuration.

- Tests: `scripts/test.sh`. Slack and Claude are faked; tests must never need network, tokens or a real server.
- Every setting is an env var read in [`config.py`](config.py). Add new ones there, in [`.env.example`](.env.example) and in the README table. Keep deployment-specific values (IDs, hosts, URLs) in config, never in code.
- [`claudino_instructions.md`](claudino_instructions.md) is the shared system prompt. Team-specific instructions belong in the server's `~/.config/claude-relay/instructions.md`.
- Merges to `main` deploy to production: keep `deploy/deployer.py` and `scripts/deploy.sh` rollback-safe.

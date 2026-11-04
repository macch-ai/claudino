#!/usr/bin/env bash
# Install, test and restart claudino from the current checkout.
# Usage: scripts/deploy.sh <previous-commit>  (rolls back to it if anything fails)
set -euo pipefail
previous="${1:-}"
cd "$(dirname "$0")/.."
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

rollback() {
  echo "FAILED: $1"
  if [[ -n "$previous" ]]; then
    echo "rolling back to ${previous:0:7}"
    git reset --hard --quiet "$previous"
    systemctl --user restart claude-relay
  fi
  exit 1
}

echo "deploying $(git rev-parse --short HEAD) (was ${previous:0:7})"
if [[ -x .venv/bin/pip ]]; then
  .venv/bin/pip install --quiet -r requirements.txt || rollback "dependency install"
else
  uv pip install --quiet --python .venv/bin/python -r requirements.txt || rollback "dependency install"
fi
if ! out=$(.venv/bin/python -m unittest discover -s tests -t . 2>&1); then
  echo "$out" | tail -30
  rollback "tests"
fi
if ! cmp -s deploy/claude-relay.service ~/.config/systemd/user/claude-relay.service; then
  cp deploy/claude-relay.service ~/.config/systemd/user/claude-relay.service
  systemctl --user daemon-reload
fi
started=$(date '+%Y-%m-%d %H:%M:%S')
systemctl --user restart claude-relay
for _ in $(seq 1 20); do
  journalctl --user -u claude-relay --since "$started" --no-pager | grep -q "Bolt app is running" && break
  sleep 1
done
if ! systemctl --user is-active --quiet claude-relay || \
   ! journalctl --user -u claude-relay --since "$started" --no-pager | grep -q "Bolt app is running"; then
  journalctl --user -u claude-relay --since "$started" --no-pager | tail -20
  rollback "relay did not come up"
fi
echo "claudino $(git rev-parse --short HEAD) is running"

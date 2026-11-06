#!/usr/bin/env bash
# Install, test and restart claudino from the current checkout.
# Usage: scripts/deploy.sh <previous-commit>  (rolls back to it if anything fails)
set -euo pipefail
previous="${1:-}"
cd "$(dirname "$0")/.."
export XDG_RUNTIME_DIR="/run/user/$(id -u)"

# Two layouts: the relay as a systemd user unit of this user, or (after deploy/setup-relay-user.sh)
# the system unit claudino.service, which this user may restart through sudo.
if [[ -f /etc/systemd/system/claudino.service ]]; then
  restart_relay() { sudo -n systemctl restart claudino.service; }
  relay_active() { systemctl is-active --quiet claudino.service; }
  relay_logs() { journalctl -u claudino.service --since "$1" --no-pager; }
  sync_unit() { :; }  # the system unit is installed by deploy/setup-relay-user.sh
else
  restart_relay() { systemctl --user restart claude-relay; }
  relay_active() { systemctl --user is-active --quiet claude-relay; }
  relay_logs() { journalctl --user -u claude-relay --since "$1" --no-pager; }
  sync_unit() {
    if ! cmp -s deploy/claude-relay.service ~/.config/systemd/user/claude-relay.service; then
      cp deploy/claude-relay.service ~/.config/systemd/user/claude-relay.service
      systemctl --user daemon-reload
    fi
  }
fi

rollback() {
  echo "FAILED: $1"
  if [[ -n "$previous" ]]; then
    echo "rolling back to ${previous:0:7}"
    git reset --hard --quiet "$previous"
    restart_relay
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
sync_unit
started=$(date '+%Y-%m-%d %H:%M:%S')
restart_relay
for _ in $(seq 1 20); do
  relay_logs "$started" | grep -q "Bolt app is running" && break
  sleep 1
done
if ! relay_active || ! relay_logs "$started" | grep -q "Bolt app is running"; then
  relay_logs "$started" | tail -20
  rollback "relay did not come up"
fi
echo "claudino $(git rev-parse --short HEAD) is running"

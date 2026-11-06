#!/usr/bin/env bash
# Move claudino's relay into its own Unix user, so Claude sessions cannot read its Slack tokens.
#
# Before: the relay, the deployer and every Claude session run as <agent>, who owns the tokens.
# After:  the user "claudino" runs the relay and the deployer as system units and alone can read the
#         tokens. It starts Claude as <agent> through sudo, so sessions keep <agent>'s logins and tools.
#
# Run once on the server, from <agent>'s checkout:   sudo deploy/setup-relay-user.sh <agent>
# Back to the old layout (user units of <agent>):     sudo deploy/setup-relay-user.sh --undo <agent>
# If the new relay does not come up, the script undoes itself.
set -euo pipefail

relay=claudino
relay_home=/home/$relay
undo=false
if [[ "${1:-}" == "--undo" ]]; then undo=true; shift; fi
agent="${1:?usage: sudo $0 [--undo] <user that runs Claude>}"
[[ $EUID -eq 0 ]] || { echo "Run it with sudo."; exit 1; }
agent_home=$(getent passwd "$agent" | cut -d: -f6)
[[ -n "$agent_home" ]] || { echo "No user $agent."; exit 1; }
agent_uid=$(id -u "$agent")
src="$(cd "$(dirname "$0")/.." && pwd)"
moved=$relay_home/.config/claude-relay/moved-from-$agent  # the agent's secrets, kept for --undo

as_agent() { runuser -u "$agent" -- env HOME="$agent_home" XDG_RUNTIME_DIR="/run/user/$agent_uid" "$@"; }
as_relay() { runuser -u "$relay" -- env HOME="$relay_home" PATH=/usr/local/bin:/usr/bin:/bin "$@"; }
step() { echo "== $*"; }

undo_setup() {
  step "stopping the system units"
  systemctl disable --now claudino.service claudino-deployer.service 2>/dev/null || true
  rm -f /etc/systemd/system/claudino.service /etc/systemd/system/claudino-deployer.service
  systemctl daemon-reload
  step "giving $agent its settings, secrets and state back"
  install -d -o "$agent" -g "$agent" -m 755 "$agent_home/.config/claude-relay"
  if [[ -f $relay_home/.config/claude-relay/env ]]; then  # the newest tokens, if they were rotated
    grep -v '^CLAUDINO_RUN_AS=' "$relay_home/.config/claude-relay/env" > "$agent_home/.config/claude-relay/env"
    chown "$agent:$agent" "$agent_home/.config/claude-relay/env"
    chmod 600 "$agent_home/.config/claude-relay/env"
  fi
  if [[ -f $relay_home/.config/claude-alert-fire/token ]]; then
    install -D -o "$agent" -g "$agent" -m 600 "$relay_home/.config/claude-alert-fire/token" \
      "$agent_home/.config/claude-alert-fire/token"
  fi
  if [[ -d $relay_home/.local/state/claude-relay ]]; then
    install -d -o "$agent" -g "$agent" "$agent_home/.local/state/claude-relay"
    cp -a "$relay_home/.local/state/claude-relay/." "$agent_home/.local/state/claude-relay/"
    chown -R "$agent:$agent" "$agent_home/.local/state/claude-relay"
  fi
  rm -f /etc/sudoers.d/claudino
  step "starting $agent's user units"
  as_agent systemctl --user enable --now claude-relay.service claudino-deployer.service
  echo "Undone. The user $relay and $relay_home are kept; remove them with: userdel -r $relay"
}

if $undo; then
  undo_setup
  exit 0
fi

step "user $relay"
id "$relay" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$relay_home" --shell /bin/bash "$relay"
chmod 750 "$relay_home"
usermod -aG "$relay" "$agent"            # Claude reads the files the relay downloads
usermod -aG systemd-journal "$relay"     # deploy.sh reads the relay's logs
usermod -aG systemd-journal "$agent"     # so you can still read them over ssh

step "uv"
command -v uv >/dev/null || install -m 755 "$agent_home/.local/bin/uv" /usr/local/bin/uv

step "checkout and venv in $relay_home/claude-relay"
if [[ ! -d $relay_home/claude-relay/.git ]]; then
  url=$(as_agent git -C "$src" remote get-url origin)
  as_relay git clone --quiet "${url/git@github.com:/https://github.com/}" "$relay_home/claude-relay"
fi
as_relay git -C "$relay_home/claude-relay" fetch --quiet origin main
as_relay bash -c 'cd ~/claude-relay && { [ -x .venv/bin/python ] || uv venv -q .venv; } &&
  uv pip install -q --python .venv/bin/python -r requirements.txt &&
  .venv/bin/python -m unittest discover -s tests -t . -q'

step "settings and state"
install -d -o "$relay" -g "$relay" -m 700 "$relay_home/.config" "$relay_home/.config/claude-relay"
for f in env instructions.md deployer.env; do
  if [[ -f $agent_home/.config/claude-relay/$f ]]; then
    install -o "$relay" -g "$relay" -m 600 "$agent_home/.config/claude-relay/$f" "$relay_home/.config/claude-relay/$f"
  fi
done
env_file=$relay_home/.config/claude-relay/env
sed -i '/^CLAUDINO_RUN_AS=/d' "$env_file"
echo "CLAUDINO_RUN_AS=$agent" >> "$env_file"
if [[ -f $agent_home/.config/claude-alert-fire/token ]]; then
  install -D -o "$relay" -g "$relay" -m 600 "$agent_home/.config/claude-alert-fire/token" \
    "$relay_home/.config/claude-alert-fire/token"
fi
if [[ -d $agent_home/.local/state/claude-relay && ! -d $relay_home/.local/state/claude-relay ]]; then
  install -d -o "$relay" -g "$relay" -m 750 "$relay_home/.local" "$relay_home/.local/state"
  cp -a "$agent_home/.local/state/claude-relay" "$relay_home/.local/state/"
  chown -R "$relay:$relay" "$relay_home/.local/state/claude-relay"
  chmod -R u+rwX,g+rX,g-w,o-rwx "$relay_home/.local/state/claude-relay"
fi

step "sudo rules"
rules=$(mktemp)
cat > "$rules" <<EOF
# deploy/setup-relay-user.sh: the relay starts Claude as $agent and restarts itself when it deploys.
$relay ALL=($agent) NOPASSWD: ALL
$relay ALL=(root) NOPASSWD: /usr/bin/systemctl restart claudino.service
EOF
visudo -cqf "$rules"
install -m 440 "$rules" /etc/sudoers.d/claudino
rm -f "$rules"

step "switching from $agent's user units to the system units"
install -m 644 "$src/deploy/system/claudino.service" "$src/deploy/system/claudino-deployer.service" \
  /etc/systemd/system/
systemctl daemon-reload
as_agent systemctl --user disable --now claude-relay.service claudino-deployer.service 2>/dev/null || true
started=$(date '+%Y-%m-%d %H:%M:%S')
systemctl enable --now claudino.service claudino-deployer.service
for _ in $(seq 1 30); do
  journalctl -u claudino.service --since "$started" --no-pager | grep -q "Bolt app is running" && break
  sleep 1
done
if ! journalctl -u claudino.service --since "$started" --no-pager | grep -q "Bolt app is running"; then
  journalctl -u claudino.service --since "$started" --no-pager | tail -20
  echo "The relay did not come up as $relay. Undoing."
  undo_setup
  exit 1
fi

step "moving the secrets out of $agent's reach"
install -d -o "$relay" -g "$relay" -m 700 "$moved"
for f in "$agent_home"/.config/claude-relay/env "$agent_home"/.config/claude-relay/env.bak* \
         "$agent_home"/.config/claude-alert-fire/token; do
  [[ -e $f ]] || continue
  mv "$f" "$moved/$(basename "$(dirname "$f")")-$(basename "$f")"
done
chown -R "$relay:$relay" "$moved"
find "$moved" -type f -exec chmod 600 {} +

cat <<EOF
Done: $relay runs the relay and the deployer; Claude runs as $agent.
Logs:    journalctl -u claudino.service -f
Tokens:  Claude sessions could read the old ones, so rotate them in the Slack app settings, then:
         cd $relay_home/claude-relay && sudo -H -u $relay scripts/set-tokens.sh
EOF

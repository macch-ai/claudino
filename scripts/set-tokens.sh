#!/usr/bin/env bash
# Store claudino's Slack tokens on this machine (hidden input, mode 600) and restart the relay.
# Other settings in the env file are kept.
set -euo pipefail
env_file=~/.config/claude-relay/env
mkdir -p "$(dirname "$env_file")"
read -rsp "App-level token (xapp-...): " app; echo
read -rsp "Bot token (xoxb-...): " bot; echo
check() { [[ "$2" == "$3"* ]] || { echo "The $1 token must start with $3. Nothing saved."; exit 1; }; }
check app-level "$app" xapp-
check bot "$bot" xoxb-
umask 077
tmp=$(mktemp "$env_file.XXXXXX")
{ grep -vE '^SLACK_(APP|BOT)_TOKEN=' "$env_file" 2>/dev/null || true
  printf 'SLACK_APP_TOKEN=%s\nSLACK_BOT_TOKEN=%s\n' "$app" "$bot"; } > "$tmp"
mv "$tmp" "$env_file"
systemctl --user restart claude-relay
echo "Saved. Relay restarted."

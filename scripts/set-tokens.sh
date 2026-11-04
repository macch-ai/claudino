#!/usr/bin/env bash
# Store claudino's Slack tokens on this machine (hidden input, mode 600) and restart the relay.
set -euo pipefail
mkdir -p ~/.config/claude-relay
read -rsp "App-level token (xapp-...): " app; echo
read -rsp "Bot token (xoxb-...): " bot; echo
check() { [[ "$2" == "$3"* ]] || { echo "The $1 token must start with $3. Nothing saved."; exit 1; }; }
check app-level "$app" xapp-
check bot "$bot" xoxb-
umask 077
printf 'SLACK_APP_TOKEN=%s\nSLACK_BOT_TOKEN=%s\n' "$app" "$bot" > ~/.config/claude-relay/env
systemctl --user restart claude-relay
echo "Saved. Relay restarted."

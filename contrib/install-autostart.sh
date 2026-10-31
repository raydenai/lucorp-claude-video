#!/usr/bin/env bash
# Start the crv Video Library dashboard (http://127.0.0.1:8770) at login, and keep it running.
# Run it yourself:   bash ~/src/lucorp-claude-video/contrib/install-autostart.sh
# Undo:              launchctl bootout "gui/$(id -u)/com.sezars.crv-library"; rm ~/Library/LaunchAgents/com.sezars.crv-library.plist
set -euo pipefail
label=com.sezars.crv-library
plist="$HOME/Library/LaunchAgents/$label.plist"
py="$HOME/.local/share/uv/tools/claude-real-video/bin/python"
mkdir -p "$HOME/.crv" "$HOME/Library/LaunchAgents"
cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>$py</string><string>-m</string><string>claude_real_video.library</string>
    <string>--root</string><string>$HOME/crv-out</string><string>--no-open</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$HOME/.crv/library.log</string>
  <key>StandardErrorPath</key><string>$HOME/.crv/library.log</string>
</dict></plist>
EOF
plutil -lint "$plist"
# free the port if a manually started dashboard is running, then hand it to launchd
pid=$(lsof -ti tcp:8770 -sTCP:LISTEN || true); [ -n "$pid" ] && kill "$pid" && sleep 1
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$plist"
sleep 2
curl -s -o /dev/null -w "dashboard: HTTP %{http_code}\n" http://127.0.0.1:8770/

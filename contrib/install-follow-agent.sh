#!/usr/bin/env bash
# Write the follow LaunchAgent; loading it remains an explicit user action.
set -euo pipefail
follow="$(command -v crv-follow || true)"
[ -n "$follow" ] || { echo 'Install crv-follow and put it on PATH first.' >&2; exit 1; }
python3 - "$follow" <<'PY'
import os
from pathlib import Path
import plistlib
import shlex
import sys

root = Path(os.environ.get('CRV_ROOT', '~/crv-out')).expanduser().resolve()
(root / '_crv').mkdir(parents=True, exist_ok=True)
plist = Path.home() / 'Library/LaunchAgents/com.crv.follow.plist'
plist.parent.mkdir(parents=True, exist_ok=True)
log = str(root / '_crv/follow.log')
with plist.open('wb') as stream:
    plistlib.dump({
        'Label': 'com.crv.follow',
        'ProgramArguments': [sys.argv[1], 'run'],
        'StartInterval': 1800,
        'EnvironmentVariables': {'CRV_ROOT': str(root), 'PATH': os.environ.get('PATH', '')},
        'StandardOutPath': log,
        'StandardErrorPath': log,
    }, stream)
print('Wrote ' + str(plist))
print('launchctl bootstrap gui/' + str(os.getuid()) + ' ' + shlex.quote(str(plist)))
PY

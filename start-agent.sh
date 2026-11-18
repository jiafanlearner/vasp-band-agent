#!/usr/bin/env bash
# Run from Git Bash. Scope the SSH selection to this process and its children.
set -euo pipefail

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
git_ssh_dir='/c/Program Files/Git/usr/bin'
if [[ ! -x "$git_ssh_dir/ssh.exe" ]]; then
    printf '%s\n' 'Git SSH not found at C:/Program Files/Git/usr/bin/ssh.exe.' >&2
    printf '%s\n' 'Update git_ssh_dir in start-agent.sh to your Git installation.' >&2
    exit 1
fi

export PATH="$git_ssh_dir:$PATH"
cd -- "$project_dir"
# Verify the executable Python will actually resolve before opening the Agent.
python -c 'import os, shutil, sys; expected="C:/Program Files/Git/usr/bin/ssh.exe"; actual=shutil.which("ssh"); print("SSH:", actual, flush=True); sys.exit(0 if actual and os.path.samefile(actual, expected) else "SSH executable mismatch; Agent was not started.")'
exec python ai_agent.py --profile relay "$@"

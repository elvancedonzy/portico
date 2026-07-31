#!/bin/sh
set -e

# Read SUPERVISOR_TOKEN from s6 envdir (shell var not yet populated at this stage)
# and write to file so cron jobs can source it (crond doesn't inherit container env)
_token=$(cat /run/s6/container_environment/SUPERVISOR_TOKEN 2>/dev/null || printenv SUPERVISOR_TOKEN 2>/dev/null || echo "")
echo "export SUPERVISOR_TOKEN=${_token}" > /data/runtime_env.sh
chmod 600 /data/runtime_env.sh
unset _token

echo "[run.sh] running initial seam_sync.py" >&2
python3 /seam_sync.py >> /data/seam_sync.log 2>&1 || \
  echo "[run.sh] initial sync failed (non-fatal, cron will retry)" >&2

echo "[run.sh] starting crond" >&2
crond -b -L /dev/stderr

echo "[run.sh] starting server.py" >&2
exec python3 /server.py

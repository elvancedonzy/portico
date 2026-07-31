#!/bin/sh
LOG=/data/seam_sync.log
if [ -f "$LOG" ]; then
  tail -n 10000 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
fi

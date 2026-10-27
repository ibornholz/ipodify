#!/usr/bin/env bash
# Start iPodify from anywhere. On a crash, keep the window open so the error stays readable.
here="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$here" || exit 1
.venv/bin/python -m ipodify "$@"
code=$?
if [[ $code -ne 0 && -t 0 ]]; then
  echo
  echo "iPodify wurde mit Fehler $code beendet – Enter zum Schließen."
  read -r
fi
exit $code

#!/usr/bin/env bash
# One run of the engine on GitHub's runners (reset item 12). This run's page is public, and the engine's own
# output names sources and companies, so none of it is printed here: it goes to a file the app keeps
# privately (send-log), and this prints one fixed line saying how the run went, by its exit
# (engine/src/jobbot_engine/scheduler/on_github.py). Published to the engine's public repo as run.sh.
set -u

if [ -z "${JOBBOT_API_URL:-}" ] || [ -z "${JOBBOT_ENGINE_TOKEN:-}" ]; then
  echo "This branch has no address or token for the app, so there was no run."
  exit 1
fi
log="${RUNNER_TEMP:-/tmp}/engine.log"
started=$SECONDS
uv run --project engine --frozen --no-dev jobbot-engine run-on-github >"$log" 2>&1
status=$?
took=$((SECONDS - started))
if ! uv run --project engine --frozen --no-dev jobbot-engine send-log "$log" >/dev/null 2>&1; then
  echo "The run's log wasn't sent to the app."
fi
case $status in
  0) echo "The run finished and its state was kept (${took} s)." ;;
  1) echo "The run couldn't start (${took} s)." ;;
  2) echo "There was no state to start from, so there was no run (${took} s)." ;;
  3) echo "The run finished, but its state wasn't kept (${took} s)." ;;
  *) echo "The run stopped unexpectedly (exit ${status}, ${took} s)." ;;
esac
exit "$status"

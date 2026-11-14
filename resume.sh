#!/usr/bin/env bash
# Makes the queued tailored resumes on GitHub's runners. This page is public, and a resume's making names the
# person's work and the posting, so none of it is printed here: it goes to a file the app keeps privately
# (send-log), and this prints one fixed line saying how it went. Each resume's own outcome goes back to the
# app with it (engine/src/jobbot_engine/resume/serve.py). Published to the engine's public repo as resume.sh.
set -u

if [ -z "${JOBBOT_API_URL:-}" ] || [ -z "${JOBBOT_ENGINE_TOKEN:-}" ]; then
  echo "This branch has no address or token for the app, so no resumes were made."
  exit 1
fi
log="${RUNNER_TEMP:-/tmp}/resume.log"
started=$SECONDS
uv run --project engine --frozen --no-dev --extra resume jobbot-engine resume >"$log" 2>&1
status=$?
took=$((SECONDS - started))
if ! uv run --project engine --frozen --no-dev jobbot-engine send-log "$log" >/dev/null 2>&1; then
  echo "The log wasn't sent to the app."
fi
case $status in
  0) echo "The queued resumes were made and answered, each done or failed in the app (${took} s)." ;;
  1) echo "The resumes couldn't start (${took} s)." ;;
  *) echo "Making the resumes stopped unexpectedly (exit ${status}, ${took} s)." ;;
esac
exit "$status"

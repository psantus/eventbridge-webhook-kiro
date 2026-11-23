#!/usr/bin/env bash
# Poll SQS; for each message, start a detached headless Kiro CLI session,
# wait until it has a session ID, then delete (ack) the message.
# Required: QUEUE_URL. Optional: WORKDIR, KIRO_TRUST_TOOLS, RUNS_DIR,
# SESSION_WAIT (seconds to wait for a session ID, default 60),
# ONCE (set to 1 to process one poll and exit).
# Auth: KIRO_API_KEY if set, otherwise the current `kiro-cli login`.
# Resume a run interactively: cd "$WORKDIR" && kiro-cli chat --v3 --resume-id <session>
# (the session ID is in $RUNS_DIR/runs.tsv). Wait for "runFinished" in the
# run's .jsonl before resuming, so two processes don't drive the same session.
set -euo pipefail

: "${QUEUE_URL:?set QUEUE_URL}"
WORKDIR="${WORKDIR:-$PWD}"
KIRO_TRUST_TOOLS="${KIRO_TRUST_TOOLS:-read,grep_search}"
RUNS_DIR="${RUNS_DIR:-$PWD/kiro-runs}"
SESSION_WAIT="${SESSION_WAIT:-60}"
ONCE="${ONCE:-0}"
mkdir -p "$RUNS_DIR"
LOG_FILE="$RUNS_DIR/poller.log"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE" >&2; }

running=1
trap 'running=0; log "signal received, stopping after current message"' INT TERM

while [ "$running" -eq 1 ]; do
  msg=$(aws sqs receive-message \
    --queue-url "$QUEUE_URL" \
    --max-number-of-messages 1 \
    --wait-time-seconds 20 \
    --visibility-timeout $((SESSION_WAIT + 60)) \
    --output json) || {
    [ "$running" -eq 1 ] || break
    log "receive-message failed"; sleep 5; continue
  }

  handle=$(jq -r '.Messages[0].ReceiptHandle // empty' <<<"$msg")
  if [ -z "$handle" ]; then
    [ "$ONCE" = "1" ] && break
    continue
  fi

  id=$(jq -r '.Messages[0].MessageId' <<<"$msg")
  prompt=$(jq -r '.Messages[0].Body | fromjson? | .prompt // empty' <<<"$msg")
  label=$(jq -r '.Messages[0].Body | fromjson? | "\(.repo // "?")#\(.number // "?") \(.action // "")"' <<<"$msg")
  if [ -z "$prompt" ]; then
    log "message $id has no prompt field, leaving it for the DLQ"
    [ "$ONCE" = "1" ] && break
    continue
  fi

  out="$RUNS_DIR/$id.jsonl"
  err="$RUNS_DIR/$id.err"
  # Detached: own stdin from a file, nohup, not waited on. The poller's
  # INT/TERM trap does not reach it because it ignores SIGHUP and runs in
  # the background (Ctrl-C only signals the foreground job).
  printf '%s' "$prompt" > "$RUNS_DIR/$id.prompt"
  (cd "$WORKDIR" || exit 1
   nohup kiro-cli chat --v3 --no-interactive \
      --trust-tools="$KIRO_TRUST_TOOLS" --output-format stream-json \
      < "$RUNS_DIR/$id.prompt" > "$out" 2> "$err" &
   echo $! > "$RUNS_DIR/$id.pid")
  pid=$(cat "$RUNS_DIR/$id.pid")

  session=""
  for _ in $(seq "$SESSION_WAIT"); do
    session=$(grep -m1 -o '"sessionId":"[^"]*"' "$out" 2>/dev/null | cut -d'"' -f4 || true)
    [ -n "$session" ] && break
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done

  if [ -z "$session" ]; then
    kill "$pid" 2>/dev/null || true
    log "message $id ($label): no session ID after ${SESSION_WAIT}s (see $err), keeping message for retry"
  elif aws sqs delete-message --queue-url "$QUEUE_URL" --receipt-handle "$handle"; then
    printf '%s\t%s\t%s\t%s\t%s\n' "$(date -u +%FT%TZ)" "$id" "$session" "$pid" "$label" >> "$RUNS_DIR/runs.tsv"
    log "message $id ($label): started session $session (pid $pid), deleted"
  else
    log "message $id ($label): session $session started but delete-message failed, it may run again"
  fi
  [ "$ONCE" = "1" ] && break
done
log "stopped"

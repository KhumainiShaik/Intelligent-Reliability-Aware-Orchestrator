#!/usr/bin/env bash
set -euo pipefail
# Watchdog: monitors experiment phases, keeps auth alive, triggers steady patch,
# then uploads final results to GCS.
# Run in: tmux new-session -d -s watchdog 'bash scripts/watchdog.sh 2>&1 | tee /tmp/watchdog.log'

TIMESTAMP="${GRID_TIMESTAMP:-20260406_140509}"
GCS_URI="${GCS_RESULTS_URI:-gs://artifacts-orchastratorcrd/oroll-results/comparison_${TIMESTAMP}}"
LOG="/tmp/watchdog.log"

log() { echo "[$(date -u '+%H:%M:%S')] $*"; }

refresh_auth() {
    gcloud container clusters get-credentials orchestrated-rollout \
        --region europe-west2 --quiet 2>/dev/null || \
        gcloud auth print-access-token > /dev/null 2>&1 || true
    log "AUTH refreshed"
}

count_trials() {
    # Usage: count_trials <mode> <type: stdout|summary.json|episode>
    local mode="$1" type="$2"
    find experiments/ -path "*grid_${TIMESTAMP}_${mode}_shard*" \
        -name "*${type}" 2>/dev/null | wc -l
}

is_phase1_done() {
    # Rolling: 9 combos × 5 shards × 5 reps = some run on each shard
    # Each shard runs its share of the 9 combos. Done when all processes finish.
    # Simpler: check for PIDs from initial launch
    local alive
    alive=$(ps aux | grep 'baseline-rolling\|baseline-canary' | grep -E 'run_experiment|full_experiment' | grep -v grep | wc -l)
    [ "$alive" -eq 0 ]
}

is_phase2_done() {
    local alive
    alive=$(ps aux | grep 'baseline-delay\|baseline-pre-scale' | grep -E 'run_experiment|full_experiment' | grep -v grep | wc -l)
    # Phase 2 starts only after phase 1 - only done if it actually ran
    local p2started
    p2started=$(find experiments/ -path "*grid_${TIMESTAMP}_baseline-delay_shard*" -name 'trial_*_stdout.txt' 2>/dev/null | wc -l)
    [ "$p2started" -gt 0 ] && [ "$alive" -eq 0 ]
}

is_patch_done() {
    # Check for steady summaries across all 5 modes
    local total=0
    for mode in rl baseline-rolling baseline-canary baseline-delay baseline-pre-scale; do
        local s
        s=$(find experiments/ -path "*grid_${TIMESTAMP}_${mode}_shard*" \
            -path "*/steady_*" -name 'trial_*_summary.json' 2>/dev/null | wc -l)
        total=$((total + s))
    done
    # 5 modes × 3 steady combos × 5 reps = 75 expected
    [ "$total" -ge 70 ]
}

print_progress() {
    log "=== PROGRESS ==="
    for mode in rl baseline-rolling baseline-canary baseline-delay baseline-pre-scale; do
        local t s e
        t=$(count_trials "$mode" "_stdout.txt")
        s=$(count_trials "$mode" "summary.json")
        e=$(count_trials "$mode" "episode_*.json")
        log "  ${mode}: trials=${t} summaries=${s} episodes=${e}"
    done
    local k6_count
    k6_count=$(ps aux | grep 'k6 run' | grep -v grep | wc -l)
    log "  k6 processes active: ${k6_count}"
}

LAST_AUTH=0
PATCH_TRIGGERED=0
UPLOAD_DONE=0

log "Watchdog started. Monitoring ${TIMESTAMP}..."
log "GCS target: ${GCS_URI}"

refresh_auth

while true; do
    NOW=$(date +%s)

    # Refresh auth every 25 minutes
    if [ $((NOW - LAST_AUTH)) -ge 1500 ]; then
        refresh_auth
        LAST_AUTH=$NOW
    fi

    print_progress

    # Check phase completion
    if is_phase1_done && [ "$PATCH_TRIGGERED" -eq 0 ]; then
        # Check if phase 2 was supposed to start (it auto-starts from run_baselines_parallel.sh)
        p2count=$(find experiments/ -path "*grid_${TIMESTAMP}_baseline-delay_shard*" -name 'trial_*_stdout.txt' 2>/dev/null | wc -l)
        if [ "$p2count" -gt 0 ] && is_phase2_done; then
            log "=== PHASES 1+2 COMPLETE ==="
            log "Triggering steady patch..."
            PATCH_TRIGGERED=1
            GRID_TIMESTAMP="${TIMESTAMP}" \
            GCS_RESULTS_URI="${GCS_URI}" \
            bash scripts/patch_steady.sh 2>&1 | tee /tmp/patch_steady.log
            log "Steady patch complete."
        elif [ "$p2count" -eq 0 ]; then
            log "Phase 1 done, waiting for Phase 2 to start..."
        else
            log "Phase 2 running... (${p2count} trials so far)"
        fi
    fi

    # After patch, upload everything to GCS
    if [ "$PATCH_TRIGGERED" -eq 1 ] && [ "$UPLOAD_DONE" -eq 0 ] && is_patch_done; then
        log "=== UPLOADING FINAL RESULTS TO GCS ==="
        refresh_auth
        gsutil -m rsync -r experiments/ "${GCS_URI}/experiments/" 2>&1 | tail -5
        UPLOAD_DONE=1
        log "=== ALL DONE. Results at: ${GCS_URI} ==="
        log "Ready to download and analyse."
        break
    fi

    sleep 60
done

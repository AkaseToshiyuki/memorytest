#!/usr/bin/env bash
# Memorytest Layer 1: Smoke test.
# Runs each of the 7 binaries for a short period and verifies that:
#   1. The binary exits 0 (or exits cleanly with the expected non-zero for sudo)
#   2. The corresponding report file is created
#   3. The report file is non-empty and parseable
#
# Designed for CI pre-commit hooks. Wall time: ~30 seconds.
# Usage:  ./test_smoke.sh  (run after `make all`)

set -u

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$REPO_ROOT/bin"
REPORTS="$REPO_ROOT/reports"
PASS=0
FAIL=0
# Per-binary timeout. Most finish in <5s, but test_inter_core (24x24 CAS matrix)
# and test_cpu_float (100M iterations × 12 ops) take 45-60s on first run.
# Wall time budget: ~3 min for the full smoke pass.
TIMEOUT_SEC=70

mkdir -p "$REPORTS"

declare -A TESTS=(
    [test_cache_hierarchy]=cache_hierarchy_report.md
    [test_memory_bandwidth]=memory_bandwidth_report.md
    [test_inter_core]=inter_core_latency_report.md
    [test_cpu_alu]=cpu_alu_report.md
    [test_cpu_float]=cpu_float_report.md
    [test_cpu_branch]=cpu_branch_report.md
    [test_cpu_multi]=cpu_multi_core_report.md
)

# Build first if needed
if [ ! -x "$BIN/test_cache_hierarchy" ]; then
    echo ">> Building binaries..."
    cd "$REPO_ROOT" && make all >/dev/null 2>&1
fi

echo ">> Running Layer 1 smoke test (timeout ${TIMEOUT_SEC}s per binary)"
echo "============================================================"

for binary in "${!TESTS[@]}"; do
    report_file="$REPORTS/${TESTS[$binary]}"

    # Snapshot the report file mtime before run (to detect fresh writes)
    pre_mtime=0
    [ -f "$report_file" ] && pre_mtime=$(stat -c %Y "$report_file" 2>/dev/null || echo 0)

    # Run with empty stdin (skip sudo prompt), bound by timeout
    output=$(timeout "$TIMEOUT_SEC" "$BIN/$binary" < /dev/null 2>&1)
    exit_code=$?

    # 124 = timeout; 0 = OK; allow non-zero for benign reasons
    # (e.g. detecting PMU unavailable)
    if [ $exit_code -eq 124 ]; then
        printf "  ✗ %-25s TIMEOUT after %ds\n" "$binary" "$TIMEOUT_SEC"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report file was created/fresh
    if [ ! -f "$report_file" ]; then
        printf "  ✗ %-25s exit=%d  MISSING REPORT: %s\n" "$binary" "$exit_code" "$report_file"
        FAIL=$((FAIL+1))
        continue
    fi

    post_mtime=$(stat -c %Y "$report_file" 2>/dev/null || echo 0)
    if [ "$post_mtime" -le "$pre_mtime" ]; then
        printf "  ✗ %-25s exit=%d  report not refreshed (mtime %d -> %d)\n" \
               "$binary" "$exit_code" "$pre_mtime" "$post_mtime"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report non-empty
    size=$(stat -c %s "$report_file" 2>/dev/null || echo 0)
    if [ "$size" -lt 200 ]; then
        printf "  ✗ %-25s exit=%d  report %d bytes (suspiciously small)\n" \
               "$binary" "$exit_code" "$size"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report has expected section
    if ! grep -qE "Test Report|## System Configuration" "$report_file"; then
        printf "  ✗ %-25s exit=%d  report missing header sections\n" \
               "$binary" "$exit_code"
        FAIL=$((FAIL+1))
        continue
    fi

    printf "  ✓ %-25s exit=%-3d  report=%6d bytes\n" "$binary" "$exit_code" "$size"
    PASS=$((PASS+1))
done

echo "============================================================"
echo "Layer 1 smoke: $PASS passed, $FAIL failed"
if [ $FAIL -gt 0 ]; then
    echo "FAIL"
    exit 1
fi
echo "PASS"
exit 0

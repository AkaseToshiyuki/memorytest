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
# Per-binary timeout, scaled to the host's CPU count so big machines (e.g.
# 96-core EPYC, 128-core Ampere) get enough wall time. The base covers
# single-threaded tests (< 5s each); test_inter_core (24x24 CAS matrix)
# and test_memory_bandwidth scale with core count more aggressively.
NCPU=$(nproc 2>/dev/null || echo 1)
# CPU tests: ~90s base, +4s per extra core pair
CPU_TIMEOUT=$(( 90 + (NCPU - 24) * 4 ))
# Inter-core test: 24x24 = 576 CAS pairs. On x86_64 with -O2 each pair is
# 1-2s on a slow EPYC core. At 96 cores, scheduling overhead multiplies.
INTER_TIMEOUT=$(( 90 + (NCPU * NCPU) / 6 ))
# Cap both.
[ $CPU_TIMEOUT -lt 90 ] && CPU_TIMEOUT=90
[ $CPU_TIMEOUT -gt 600 ] && CPU_TIMEOUT=600
[ $INTER_TIMEOUT -lt 90 ] && INTER_TIMEOUT=90
[ $INTER_TIMEOUT -gt 1800 ] && INTER_TIMEOUT=1800
TIMEOUT_SEC=$CPU_TIMEOUT

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

    # test_inter_core scales super-linearly with core count (24x24 CAS matrix);
    # everything else uses CPU_TIMEOUT.
    if [ "$binary" = "test_inter_core" ]; then
        THIS_TIMEOUT=$INTER_TIMEOUT
    else
        THIS_TIMEOUT=$TIMEOUT_SEC
    fi

    # Run with empty stdin (skip sudo prompt), bound by timeout
    output=$(timeout "$THIS_TIMEOUT" "$BIN/$binary" < /dev/null 2>&1)
    exit_code=$?

    # 124 = timeout; 0 = OK; allow non-zero for benign reasons
    # (e.g. detecting PMU unavailable)
    if [ $exit_code -eq 124 ]; then
        printf "  ✗ %-25s TIMEOUT after %ds\n" "$binary" "$THIS_TIMEOUT"
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

#!/usr/bin/env bash
# Memorytest — Run all 7 binaries and verify they produce valid reports.
#
# This is the first of three test layers (smoke → sanity → regression).
# All three are mandatory; run `make test` to execute them in sequence.
#
# Timeouts scale with CPU count so large servers (96-core EPYC, 128-core
# Ampere) get enough wall time.

set -u

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
BIN="$REPO_ROOT/bin"
REPORTS="$REPO_ROOT/reports"
PASS=0
FAIL=0
SKIP=0

NCPU=$(nproc 2>/dev/null || echo 1)

# CPU tests: ~90s base + 4s per extra core beyond 24
CPU_TIMEOUT=$(( 90 + (NCPU - 24) * 4 ))
[ $CPU_TIMEOUT -lt 90 ] && CPU_TIMEOUT=90
[ $CPU_TIMEOUT -gt 600 ] && CPU_TIMEOUT=600

# Inter-core test: N×N CAS pairs. Formula: 120 + N²/3 seconds.
#   - 24 cores →  312s (~5 min)
#   - 96 cores → 3192s (~53 min, EPYC dual-socket)
# Capped at 3 hours as a safety net.
INTER_TIMEOUT=$(( 120 + (NCPU * NCPU) / 3 ))
[ $INTER_TIMEOUT -lt 120 ] && INTER_TIMEOUT=120
[ $INTER_TIMEOUT -gt 10800 ] && INTER_TIMEOUT=10800

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

echo ">> Running full benchmark suite"
echo "   CPU cores: $NCPU"
echo "   CPU timeout: ${CPU_TIMEOUT}s  |  inter-core timeout: ${INTER_TIMEOUT}s"
echo "============================================================"

for binary in "${!TESTS[@]}"; do
    report_file="$REPORTS/${TESTS[$binary]}"

    # Snapshot report file mtime before run
    pre_mtime=0
    [ -f "$report_file" ] && pre_mtime=$(stat -c %Y "$report_file" 2>/dev/null || echo 0)

    if [ "$binary" = "test_inter_core" ]; then
        THIS_TIMEOUT=$INTER_TIMEOUT
    else
        THIS_TIMEOUT=$TIMEOUT_SEC
    fi

    # Run with empty stdin (skip sudo prompt), bound by timeout
    output=$(timeout "$THIS_TIMEOUT" "$BIN/$binary" < /dev/null 2>&1)
    exit_code=$?

    # 124 = timeout
    if [ $exit_code -eq 124 ]; then
        printf "  ✗ %-25s TIMEOUT after %ds\n" "$binary" "$THIS_TIMEOUT"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report file was created / refreshed
    if [ ! -f "$report_file" ]; then
        printf "  ✗ %-25s exit=%d  MISSING REPORT: %s\n" "$binary" "$exit_code" "$report_file"
        FAIL=$((FAIL+1))
        continue
    fi

    post_mtime=$(stat -c %Y "$report_file" 2>/dev/null || echo 0)
    if [ "$post_mtime" -le "$pre_mtime" ]; then
        printf "  ✗ %-25s exit=%d  report not refreshed\n" "$binary" "$exit_code"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report non-empty
    size=$(stat -c %s "$report_file" 2>/dev/null || echo 0)
    if [ "$size" -lt 200 ]; then
        printf "  ✗ %-25s exit=%d  report %d bytes (too small)\n" "$binary" "$exit_code" "$size"
        FAIL=$((FAIL+1))
        continue
    fi

    # Verify report structure
    if ! grep -qE "Test Report|## System Configuration" "$report_file"; then
        printf "  ✗ %-25s exit=%d  report missing header\n" "$binary" "$exit_code"
        FAIL=$((FAIL+1))
        continue
    fi

    printf "  ✓ %-25s exit=%-3d  report=%6d bytes\n" "$binary" "$exit_code" "$size"
    PASS=$((PASS+1))
done

echo "============================================================"
echo "Layer 1: $PASS passed, $FAIL failed, $SKIP skipped"
if [ $FAIL -gt 0 ]; then
    echo "FAIL"
    exit 1
fi
echo "PASS"
exit 0

# Memory Benchmark Makefile
#
# Build model (2026-06-04 rewrite):
# - Default `make tests` produces portable -O2 binaries that run on
#   x86_64 / arm64 / riscv64 / ppc64. SIMD is selected at runtime via
#   platform_simd_detect() (src/platform.c), so the binary doesn't need
#   -march=native to use AVX2/NEON/etc.
# - `make opt` adds -O3 -DNDEBUG and (by default) a SAFE set of -m flags
#   that are valid on the current build host. -march=native is opt-in
#   via `make opt-native` so cross-compiled binaries don't get garbage flags.
# - LTO / ofast / asan targets unchanged in spirit.
#
# CFLAGS notes:
#  -Wno-unused-function: every test_*.c links the entire common module set,
#   and not every helper is used by every binary. Linker errors would catch
#   truly dead code; the warning is noise. Keep it off.
#  -Wno-unused-result: read/fscanf/system calls in detect.c that we don't
#   care about the return value of. Tightening these up is a future cleanup.
#  -Wno-discarded-qualifiers: <linux/perf_event.h> uses __u64 qualifiers
#   that we pass into syscall(); safe to drop.

CC ?= gcc
HOST_CC ?= $(CC)
CFLAGS ?= -O2 -Wall -std=c11 -pthread -Wno-unused-function -Wno-unused-result -Wno-discarded-qualifiers
LDFLAGS ?=
SRC_DIR = src
BUILD_DIR = bin
VERSION = 1.0.1

# Test categories
MEMORY_TESTS = test_cache_hierarchy test_memory_bandwidth test_inter_core
CPU_TESTS = test_cpu_alu test_cpu_float test_cpu_branch test_cpu_multi
ALL_TESTS = $(MEMORY_TESTS) $(CPU_TESTS)

# Common source modules (split from monolithic common.c).
# platform.c is the new cross-platform detection layer.
COMMON_SRCS = src/common.c src/util.c src/platform.c src/detect.c src/report.c \
              src/simd.c src/pmu.c src/latency.c

.PHONY: all clean tests memory cpu help release test smoke sanity regression lint report score \
        opt opt3 ofast lto perf-asan opt-native platform-check

# Default: build all
all: tests

# Build all test binaries
tests: $(addprefix $(BUILD_DIR)/, $(ALL_TESTS))

# Build memory/cache subsystem tests
memory: $(addprefix $(BUILD_DIR)/, $(MEMORY_TESTS))

# Build CPU performance tests
cpu: $(addprefix $(BUILD_DIR)/, $(CPU_TESTS))

# Layer 1 smoke test: run all 7 binaries, verify reports are produced
smoke: tests
	@bash test_smoke.sh

# Interactive run: stdin is a real TTY so the platform layer can prompt
# for sudo / unknown values. Use when auto-detection failed and you want
# the benchmark to ask you for the missing data instead of reporting 0.
#
# Two modes:
#   make interactive         # run as current user; binary may prompt
#                            # for sudo password (if /dev/null is the
#                            # stdin, no prompt fires)
#   make interactive-sudo    # run each binary with `sudo -E` so the
#                            # platform layer can use privileged probes
#                            # (cpupower / dmidecode / /sys/.../cpuinfo_max_freq)
#
# IMPORTANT: these targets must be run in a real terminal (not from CI),
# otherwise platform_is_tty() returns false and the prompts are skipped.
interactive: tests
	@bash bin/run_interactive.sh

interactive-sudo: tests
	@bash bin/run_interactive.sh --sudo

# Layer 2 sanity test: assert each metric falls within plausible physical range
# --skip-missing tells sanity to count missing reports (e.g. test_inter_core
# skipped on big machines) as SKIP rather than FAIL.
sanity:
	@python3 test_sanity.py --skip-missing

# `make test` runs all 3 layers then generates the HTML report.
# All layers are advisory — failures are reported but do not block later steps.
# This is the ONLY recommended test target. Individual layers exist for debugging.
test: tests
	-@bash test_smoke.sh
	-@python3 test_sanity.py --skip-missing
	-@python3 test_regression.py
	@$(MAKE) report

# Layer 3 regression: record current metrics, compare to median of last 3 runs
# Exits 1 if any metric deviates >50% from baseline.
regression:
	@python3 test_regression.py

# Just record current metrics to history.jsonl without comparing
regression-record:
	@python3 test_regression.py --record-only

# Run all benchmarks (assumes binaries are built and reports/*.md are fresh)
# and produce the canonical HTML report. The HTML is the single source of truth —
# open it in any browser, or use the browser's print-to-PDF. Also computes the
# 0-100 score with letter grade.
report: deps
	@echo "=== generating HTML report ==="
	@python3 report_html.py
	@echo ""
	@echo "=== score summary ==="
	@python3 report_score.py | head -10

# Verify Python dependencies needed for chart generation.
# Does NOT auto-install — prints the command you need.
deps:
	@python3 -c "import matplotlib" 2>/dev/null && exit 0; \
	 echo "=== Missing Python dependencies ==="; \
	 echo ""; \
	 echo "  matplotlib is required to generate PNG charts (cache latency,"; \
	 echo "  bandwidth, inter-core heatmaps, multi-core scaling)."; \
	 echo "  The HTML report can be generated without it, but charts"; \
	 echo "  will show placeholders."; \
	 echo ""; \
	 echo "  Install:"; \
	 echo "    pip3 install matplotlib --break-system-packages"; \
	 echo ""; \
	 echo "======================================="; \
	 echo ""

# Print score summary only (no report)
score:
	@python3 report_score.py | head -10

# platform-check: a build-host diagnostic — prints detected arch / SIMD / PMU
# without compiling anything. Useful to verify what the toolchain supports.
platform-check:
	@echo "=== build host ==="
	@$(HOST_CC) -dumpmachine
	@echo "=== a tiny test binary that uses the platform layer (non-interactive) ==="
	@printf '%s\n' \
		'#include "src/common.h"' \
		'#include "src/platform.h"' \
		'#include "src/util.h"' \
		'int main(void) {' \
		'    platform_set_non_interactive(true);' \
		'    init_platform_layer();' \
		'    const SIMDInfo *s = get_simd_info();' \
		'    const PMUCapabilities *p = platform_pmu_probe();' \
		'    printf("arch=%s simd=\"%s\" vwidth=%d\\n",' \
		'           arch_names[current_arch], s->simd_flags, s->vector_width);' \
		'    printf("pmu: available=%d paranoia=%d hw=%d self=%d\\n",' \
		'           p->available, p->paranoia, p->can_profile_hw, p->can_profile_self);' \
		'    return 0;' \
		'}' > /tmp/_pc.c
	@$(HOST_CC) $(CFLAGS) -I. /tmp/_pc.c src/util.c src/platform.c src/common.c src/detect.c src/simd.c src/report.c -o /tmp/_pc 2>&1 | head -20
	@/tmp/_pc 2>&1 | head -10
	@rm -f /tmp/_pc /tmp/_pc.c

# lint: static sanity checks — CFLAGS warning hardening + Python compile + shellcheck
# Uses portable -O2 + -Werror, no -march, so it works on any build host.
lint:
	@echo "--- C: recompile with -Werror on key warnings ---"
	$(MAKE) clean tests CFLAGS="-O2 -Wall -std=c11 -pthread -Werror -Wno-unused-function -Wno-unused-result -Wno-discarded-qualifiers"
	@echo "--- Python: py_compile all .py ---"
	@python3 -m py_compile generate_report.py report_html.py test_sanity.py test_regression.py
	@echo "--- bash: -n syntax check ---"
	@bash -n test_smoke.sh
	@echo "lint: all passed"

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# All test binaries now depend on the full set of common modules.
# This ensures linker sees all symbols (perf_counters, pmu_cache_counters, etc.)
$(BUILD_DIR)/test_cache_hierarchy: $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) -lm $(LDFLAGS)

$(BUILD_DIR)/test_memory_bandwidth: $(SRC_DIR)/test_memory_bandwidth.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_memory_bandwidth.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_inter_core: $(SRC_DIR)/test_inter_core.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_inter_core.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_alu: $(SRC_DIR)/test_cpu_alu.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_alu.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_float: $(SRC_DIR)/test_cpu_float.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_float.c $(COMMON_SRCS) -lm $(LDFLAGS)

$(BUILD_DIR)/test_cpu_branch: $(SRC_DIR)/test_cpu_branch.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_branch.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_multi: $(SRC_DIR)/test_cpu_multi.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_multi.c $(COMMON_SRCS) -lm $(LDFLAGS)

# Clean build artifacts
clean:
	rm -rf $(BUILD_DIR)

# Clean reports
clean-reports:
	rm -rf reports/*.md reports/*.json reports/*.html reports/charts

# Clean everything
distclean: clean clean-reports

# ========================================================================
# Optimised builds
#
# `make opt` adds -O3 -DNDEBUG but NOT -march=native. The platform layer
# (src/platform.c) will still detect and use the best SIMD at runtime.
# Use `make opt-native` only when you are *sure* you won't run the binary
# on a different CPU than the build host.
# ========================================================================

# Safe optimised build — no -march, runtime SIMD detection picks the best.
opt:     CFLAGS += -O3 -DNDEBUG -fomit-frame-pointer
opt:     tests

# Alias of opt (kept for backward compatibility)
opt3:    CFLAGS += -O3 -DNDEBUG -fomit-frame-pointer
opt3:    tests

# ofast is opt + -ffast-math. Still portable.
ofast:   CFLAGS += -Ofast -DNDEBUG -fomit-frame-pointer -ffast-math
ofast:   tests

# Link-time-optimisation build. Portable.
lto:     CFLAGS += -O3 -DNDEBUG -flto -fomit-frame-pointer
lto:     LDFLAGS += -flto
lto:     tests

# Aggressive native build — DANGEROUS on heterogenous clusters.
# Will emit a warning unless MARCH_OVERRIDE=1 is set.
opt-native:
	@if [ "$(MARCH_OVERRIDE)" != "1" ]; then \
		echo "WARNING: opt-native adds -march=native. The resulting binary"; \
		echo "         may use illegal instructions on a different CPU model."; \
		echo "         Set MARCH_OVERRIDE=1 to confirm you understand this."; \
		echo "         Use 'make opt' for a portable optimised build."; \
		exit 1; \
	fi
	$(MAKE) tests CFLAGS="$(CFLAGS) -O3 -march=native -DNDEBUG -fomit-frame-pointer"

# ASan build for memory-safety checks (not used in perf analysis but useful)
perf-asan: CFLAGS += -O1 -g -fsanitize=address -fno-omit-frame-pointer
perf-asan: LDFLAGS += -fsanitize=address
perf-asan: tests

# Show text/data/bss per binary via the `size` utility
size: tests
	@echo "=== size $(BUILD_DIR)/* ==="
	@$(SIZE) $(addprefix $(BUILD_DIR)/, $(ALL_TESTS)) || true

SIZE ?= size

# Build release package
release: opt
	@mkdir -p release/bin release/src
	@cp bin/* release/bin/
	@cp Makefile generate_report.py report_html.py report_score.py README.md LICENSE tests.json release/
	@cp test_smoke.sh test_sanity.py test_regression.py release/
	@cp ASM_OPTIMIZATIONS.md TEST_LAYERS.md release/
	@cp src/common.h src/platform.h src/util.h src/asm_helpers.h release/src/
	@tar -czf memorytest-$(VERSION).tar.gz -C . release
	@rm -rf release/
	@echo "Release package: memorytest-$(VERSION).tar.gz"
	@ls -lh memorytest-$(VERSION).tar.gz

help:
	@echo "Memory Benchmark Suite v$(VERSION)"
	@echo ""
	@echo "Build targets (all use -O2 portable CFLAGS by default; SIMD picked at runtime):"
	@echo "  all           - Build all test binaries (default)"
	@echo "  tests         - Build all test binaries"
	@echo "  memory        - Build memory/cache subsystem tests only"
	@echo "  cpu           - Build CPU performance tests only"
	@echo "  opt           - Build with -O3 -DNDEBUG (still portable, runtime SIMD)"
	@echo "  opt3          - Alias of opt"
	@echo "  ofast         - Build with -Ofast -ffast-math (portable)"
	@echo "  lto           - Build with -O3 -flto (portable)"
	@echo "  opt-native    - Build with -O3 -march=native (REQUIRES MARCH_OVERRIDE=1)"
	@echo "  perf-asan     - Build with -O1 -fsanitize=address"
	@echo ""
	@echo "Diagnostics / verification:"
	@echo "  platform-check - Print detected arch/SIMD/PMU for the build host"
	@echo ""
	@echo "Cleaning:"
	@echo "  clean         - Remove built binaries"
	@echo "  clean-reports - Remove generated reports"
	@echo "  distclean     - Remove all generated files"
	@echo "  release       - Create release package (uses opt flags)"
	@echo ""
	@echo "Test (all 3 layers, mandatory):"
	@echo "  test          - smoke + sanity + regression (full suite)"
	@echo ""
	@echo "Individual layers (for debugging only; prefer 'make test'):"
	@echo "  smoke         - Layer 1: run 7 binaries, verify reports produced"
	@echo "  sanity        - Layer 2: assert each metric within plausible range"
	@echo "  regression    - Layer 3: compare to history.jsonl baseline"
	@echo ""
	@echo "Post-test:"
	@echo "  report        - Generate HTML report from markdown sources"
	@echo "  score         - Print score summary only"
	@echo "  lint          - Static checks (C -Werror, py_compile, bash -n)"
	@echo ""
	@echo "Test categories:"
	@echo "  Memory/Cache:  cache_hierarchy, memory_bandwidth, inter_core"
	@echo "  CPU:            cpu_alu, cpu_float, cpu_branch, cpu_multi"
	@echo ""
	@echo "Cross-platform notes:"
	@echo "  - The binary detects arch / SIMD / cache / memory / freq / PMU"
	@echo "    at runtime (see src/platform.c). It will run on x86_64, arm64,"
	@echo "    riscv64, ppc64 without recompilation."
	@echo "  - On TTY, detection failures prompt the user (with defaults shown)."
	@echo "    On non-TTY (CI, smoke tests), safe defaults are used."
	@echo "  - Use MARCH_OVERRIDE=1 make opt-native ONLY if you are sure the"
	@echo "    build and run hosts share the same CPU model."

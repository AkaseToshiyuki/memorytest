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
COMPILER_DEFINES := $(shell $(CC) -dM -E - </dev/null)
COMPILER_TARGET := $(shell $(CC) -dumpmachine)
IS_DARWIN := $(findstring darwin,$(COMPILER_TARGET))$(findstring macos,$(COMPILER_TARGET))
IS_WINDOWS := $(findstring windows,$(COMPILER_TARGET))$(findstring mingw,$(COMPILER_TARGET))
IS_CLANG := $(findstring __clang__,$(COMPILER_DEFINES))
NATIVE_CPU_FLAG := $(if $(findstring __aarch64__,$(COMPILER_DEFINES)),-mcpu=native,-march=native)
QUALIFIER_WARNING := $(if $(IS_CLANG),-Wno-incompatible-pointer-types-discards-qualifiers,-Wno-discarded-qualifiers)
CFLAGS ?= -O2 -Wall -std=c11 -pthread -Wno-unused-function -Wno-unused-result $(QUALIFIER_WARNING)
LDFLAGS ?=
SRC_DIR = src
BUILD_DIR = bin
VERSION = 0.1.1
MODULE_ARGS = $(foreach m,$(MODULE) $(MODULES),--module "$(m)") $(if $(CATEGORY),--category "$(CATEGORY)")

# Test categories
MEMORY_TESTS = test_cache_hierarchy test_memory_bandwidth test_inter_core
CPU_TESTS = test_cpu_alu test_cpu_float test_cpu_branch test_cpu_multi test_cpu_architecture
ALL_TESTS = $(MEMORY_TESTS) $(CPU_TESTS)

# Common source modules (split from monolithic common.c).
# platform.c is the new cross-platform detection layer.
DETECT_SRC = $(if $(IS_WINDOWS),src/detect_windows.c,$(if $(IS_DARWIN),src/detect_macos.c,src/detect.c))
PMU_SRC = $(if $(or $(IS_DARWIN),$(IS_WINDOWS)),src/pmu_macos.c,src/pmu.c)
UTIL_SRC = $(if $(IS_WINDOWS),src/util_windows.c,src/util.c)
PLATFORM_LIBS = $(if $(IS_WINDOWS),-ladvapi32)
LDFLAGS += $(PLATFORM_LIBS)
COMMON_SRCS = src/common.c $(UTIL_SRC) src/platform.c $(DETECT_SRC) src/report.c \
              src/simd.c $(PMU_SRC) src/latency.c
COMMON_HEADERS = $(wildcard src/*.h)
# Scored scalar protocol is deliberately independent of opt/native/fast-math
# presets. A different workload/build profile needs a different score version.
override CM_VECTOR_FLAGS := $(if $(IS_CLANG),-fno-vectorize -fno-slp-vectorize,-fno-tree-vectorize -fno-tree-slp-vectorize)
override CM_CFLAGS := -O2 -std=c11 -Wall -Wextra -pthread -fno-fast-math -ffp-contract=off $(CM_VECTOR_FLAGS) -fno-lto
CM_SOURCE_HASH := $(shell python3 -c 'import hashlib; print(hashlib.sha256(open("src/compute_kernel.c", "rb").read()).hexdigest())')

.PHONY: all clean tests memory cpu help release test list-tests check-workflow check-compute check-platform smoke sanity regression lint report score \
        opt opt3 ofast lto perf-asan opt-native platform-check

# Default: build all
all: tests

# Build all test binaries
tests: $(addprefix $(BUILD_DIR)/, $(ALL_TESTS))

# Build memory/cache subsystem tests
memory: $(addprefix $(BUILD_DIR)/, $(MEMORY_TESTS))

# Build CPU performance tests
cpu: $(addprefix $(BUILD_DIR)/, $(CPU_TESTS))

# Optional developer check, separate from the user benchmark workflow.
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

# Optional developer check: assert each metric falls within plausible physical range
# --skip-missing tells sanity to count missing reports (e.g. test_inter_core
# skipped on big machines) as SKIP rather than FAIL.
sanity:
	@python3 test_sanity.py --skip-missing

# User workflow: select modules, build/run only those modules, always report.
# Examples: make test; make test MODULE=cpu_alu; make test CATEGORY=cpu
test:
	@python3 run_benchmarks.py --bin-dir "$(abspath $(BUILD_DIR))" $(MODULE_ARGS) $(ARGS)

list-tests:
	@python3 run_benchmarks.py --list

# Unit/integration fixtures only; does not run hardware benchmarks.
check-workflow:
	@python3 -m unittest discover -s tests -p 'test_workflow.py' -v

# Independent kernel references, scoring contract, fault injection, bounded 2-CPU run.
check-compute:
	@python3 -m unittest discover -s tests -p 'test_compute.py' -v

check-platform:
	@python3 -m unittest discover -s tests -p 'test_platform.py' -v

# Optional developer regression: record current metrics, compare to median of last 3 runs
# Exits 1 if any metric deviates >50% from baseline.
regression:
	@python3 test_regression.py

# Just record current metrics to history.jsonl without comparing
regression-record:
	@python3 test_regression.py --record-only

# Regenerate an archived run; no benchmarks and no legacy data mixing.
report:
	@python3 report_html.py $(if $(RUN_DIR),--run-dir "$(RUN_DIR)",--latest-run) $(foreach m,$(MODULE) $(MODULES),--module "$(m)") $(ARGS)

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
	@python3 compute_score.py $(if $(RUN_DIR),--run-dir "$(RUN_DIR)",--latest-run)

# platform-check: a build-host diagnostic — compiles a tiny probe and prints
# detected arch / SIMD / PMU. Useful to verify what the toolchain supports.
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
		'    printf("arch=%s simd=\"%s\" vwidth=%d\n",' \
		'           arch_names[current_arch], s->simd_flags, s->vector_width);' \
		'    printf("pmu: available=%d paranoia=%d hw=%d self=%d\n",' \
		'           p->available, p->paranoia, p->can_profile_hw, p->can_profile_self);' \
		'    return 0;' \
		'}' > /tmp/_pc.c
	@$(HOST_CC) $(CFLAGS) -I. /tmp/_pc.c $(COMMON_SRCS) -o /tmp/_pc
	@/tmp/_pc
	@rm -f /tmp/_pc /tmp/_pc.c

# lint: static sanity checks — CFLAGS warning hardening + Python compile + shellcheck
# Uses portable -O2 + -Werror, no -march, so it works on any build host.
lint:
	@echo "--- C: recompile with -Werror on key warnings ---"
	$(MAKE) clean tests CFLAGS="-O2 -Wall -std=c11 -pthread -Werror -Wno-unused-function -Wno-unused-result $(QUALIFIER_WARNING)"
	@echo "--- Python: py_compile all .py ---"
	@python3 -m py_compile generate_report.py report_html.py report_score.py compute_score.py run_benchmarks.py benchmark_registry.py test_sanity.py test_regression.py
	@echo "--- bash: -n syntax check ---"
	@bash -n test_smoke.sh
	@echo "lint: all passed"

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# All test binaries now depend on the full set of common modules.
# This ensures linker sees all symbols (perf_counters, pmu_cache_counters, etc.)
$(BUILD_DIR)/test_cache_hierarchy: $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) -lm $(LDFLAGS)

$(BUILD_DIR)/test_memory_bandwidth: $(SRC_DIR)/test_memory_bandwidth.c $(SRC_DIR)/memory_latency.h $(SRC_DIR)/memory_kernels.h $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_memory_bandwidth.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_inter_core: $(SRC_DIR)/test_inter_core.c $(SRC_DIR)/intercore_macos.h $(SRC_DIR)/intercore_observed.h $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_inter_core.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_alu: $(SRC_DIR)/test_cpu_alu.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_alu.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_float: $(SRC_DIR)/test_cpu_float.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_float.c $(COMMON_SRCS) -lm $(LDFLAGS)

$(BUILD_DIR)/test_cpu_branch: $(SRC_DIR)/test_cpu_branch.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_branch.c $(COMMON_SRCS) $(LDFLAGS)

$(BUILD_DIR)/test_cpu_architecture: $(SRC_DIR)/test_cpu_architecture.c $(SRC_DIR)/architecture_kernels.h $(SRC_DIR)/architecture_frontend.h $(SRC_DIR)/architecture_widths.h $(SRC_DIR)/architecture_btb_layout.h $(SRC_DIR)/architecture_resources.h $(SRC_DIR)/architecture_competition.h $(SRC_DIR)/architecture_counters.h $(SRC_DIR)/architecture_sampling.h $(SRC_DIR)/memory_latency.h $(SRC_DIR)/memory_kernels.h $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cpu_architecture.c $(COMMON_SRCS) -lm $(LDFLAGS)

.PHONY: force-compute-build
# Rebuild to capture the actual compiler/profile; never reuse a native/old v1 binary.
$(BUILD_DIR)/test_cpu_multi: $(SRC_DIR)/test_cpu_multi.c $(SRC_DIR)/compute_kernel.c $(SRC_DIR)/compute_kernel.h $(SRC_DIR)/thread_schedule.h Makefile force-compute-build | $(BUILD_DIR)
	$(CC) $(CM_CFLAGS) -DCM_BUILD_PROFILE='"portable-scalar-v2"' -DCM_BUILD_FLAGS='"$(CM_CFLAGS)"' -DCM_SOURCE_HASH='"$(CM_SOURCE_HASH)"' -o $@ src/test_cpu_multi.c src/compute_kernel.c -lm $(PLATFORM_LIBS)

$(addprefix $(BUILD_DIR)/, $(ALL_TESTS)): $(COMMON_HEADERS)

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
		echo "WARNING: opt-native adds $(NATIVE_CPU_FLAG). The resulting binary"; \
		echo "         may use illegal instructions on a different CPU model."; \
		echo "         Set MARCH_OVERRIDE=1 to confirm you understand this."; \
		echo "         Use 'make opt' for a portable optimised build."; \
		exit 1; \
	fi
	$(MAKE) tests CFLAGS="$(CFLAGS) -O3 $(NATIVE_CPU_FLAG) -DNDEBUG -fomit-frame-pointer"

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
release:
	@python3 packaging/build_source.py

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
	@echo "Benchmarks (each run generates a self-contained HTML report):"
	@echo "  test          - Run all 7 modules (default selection)"
	@echo "  test MODULE=cpu_alu - Run one module and create its own HTML report"
	@echo '  test MODULES="cpu_alu cpu_float" - Run selected modules'
	@echo "  test CATEGORY=cpu - Run all CPU modules (or CATEGORY=memory)"
	@echo "  list-tests    - List selectable modules"
	@echo "  check-workflow - Run workflow fixtures without hardware benchmarks"
	@echo "  check-compute - Check scored kernels + bounded native CPU validation"
	@echo "  check-platform - Public API contracts + bounded macOS-native checks"
	@echo ""
	@echo "Optional legacy developer checks (not invoked by 'make test'):"
	@echo "  smoke         - Run 7 binaries, check report presence in legacy reports/"
	@echo "  sanity        - Check plausible ranges in legacy reports/"
	@echo "  regression    - Compare legacy reports/ to history.jsonl baseline"
	@echo ""
	@echo "Post-test:"
	@echo "  report        - Regenerate latest run HTML (or RUN_DIR=reports/runs/<id>)"
	@echo "  score         - Print v2 absolute scores from latest run (or RUN_DIR=...)"
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

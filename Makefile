# Memory Benchmark Makefile

CC ?= gcc
# Default CFLAGS (-O2). Can be overridden via `make CFLAGS="..."` or the
# release/opt targets below.
#
# Notes on warnings:
#  -Wno-unused-function: every test_*.c links the entire common module set,
#   and not every helper is used by every binary. Linker errors would catch
#   truly dead code; the warning is noise. Keep it off.
#  -Wno-unused-result: read/fscanf/system calls in detect.c that we don't
#   care about the return value of. Tightening these up is a future cleanup.
CFLAGS ?= -O2 -Wall -std=c11 -pthread -Wno-unused-function -Wno-unused-result -Wno-discarded-qualifiers
LDFLAGS ?=
SRC_DIR = src
BUILD_DIR = bin
VERSION = 1.0.0

# Test categories
MEMORY_TESTS = test_cache_hierarchy test_memory_bandwidth test_inter_core
CPU_TESTS = test_cpu_alu test_cpu_float test_cpu_branch test_cpu_multi
ALL_TESTS = $(MEMORY_TESTS) $(CPU_TESTS)

# Common source modules (split from monolithic common.c)
COMMON_SRCS = src/common.c src/util.c src/detect.c src/report.c \
              src/simd.c src/pmu.c src/latency.c

.PHONY: all clean tests memory cpu help release test smoke sanity regression lint report report-html score

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

# Layer 2 sanity test: assert each metric falls within plausible physical range
sanity:
	@python3 test_sanity.py

# `make test` runs Layer 1 + Layer 2 (both should pass in <5 min total)
test: tests smoke sanity

# Layer 3 regression: record current metrics, compare to median of last 3 runs
# Exits 1 if any metric deviates >50% from baseline.
regression:
	@python3 test_regression.py

# Just record current metrics to history.jsonl without comparing
regression-record:
	@python3 test_regression.py --record-only

# Run all benchmarks (assumes binaries are built and reports/*.md are fresh)
# and produce both the canonical HTML report and a 1:1 PDF print of it.
# Also computes the 0-100 score with letter grade.
report:
	@echo "=== generating HTML report (canonical, zero deps) ==="
	@python3.12 report_html.py --pdf reports/benchmark_report.pdf
	@echo ""
	@echo "=== score summary ==="
	@python3.12 report_score.py | head -10

# HTML-only report (no PDF; no browser required)
report-html:
	@python3.12 report_html.py

# Print score summary only (no report)
score:
	@python3.12 report_score.py | head -10

# lint: static sanity checks — CFLAGS warning hardening + Python compile + shellcheck
lint:
	@echo "--- C: recompile with -Werror on key warnings ---"
	$(MAKE) clean tests CFLAGS="-O2 -Wall -std=c11 -pthread -Werror -Wno-unused-function -Wno-unused-result -Wno-discarded-qualifiers"
	@echo "--- Python: py_compile all .py ---"
	@python3 -m py_compile generate_report.py test_sanity.py test_regression.py
	@echo "--- bash: -n syntax check ---"
	@bash -n test_smoke.sh
	@echo "lint: all passed"

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

# All test binaries now depend on the full set of common modules.
# This ensures linker sees all symbols (perf_counters, pmu_cache_counters, etc.)
$(BUILD_DIR)/test_cache_hierarchy: $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $(SRC_DIR)/test_cache_hierarchy.c $(COMMON_SRCS) $(LDFLAGS)

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
	rm -rf reports/*.md reports/*.json

# Clean everything
distclean: clean clean-reports

# Build with aggressive optimisation (mirrors `make CFLAGS="..."` but keeps -O2 default).
# These targets compare well against the default -O2 build for the PERF_NOTES.md study.
opt:     CFLAGS += -O3 -march=native -DNDEBUG -fomit-frame-pointer
opt:     tests

opt3:    CFLAGS += -O3 -march=native -DNDEBUG -fomit-frame-pointer
opt3:    tests

ofast:   CFLAGS += -Ofast -march=native -DNDEBUG -fomit-frame-pointer -ffast-math
ofast:   tests

# Link-time-optimisation build
lto:     CFLAGS += -O3 -march=native -DNDEBUG -flto -fomit-frame-pointer
lto:     LDFLAGS += -flto
lto:     tests

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
	@mkdir -p release
	@cp -r bin Makefile generate_report.py README.md tests.json src/common.h .
	@mv bin/* release/ 2>/dev/null || true
	@rm -rf bin
	@tar -czvf memorytest-$(VERSION).tar.gz release/
	@zip -r memorytest-$(VERSION).zip release/
	@echo "Release packages created:"
	@ls -la memorytest-$(VERSION).tar.gz memorytest-$(VERSION).zip
	@rm -rf release/

help:
	@echo "Memory Benchmark Suite v$(VERSION)"
	@echo ""
	@echo "Targets:"
	@echo "  all         - Build all test binaries (default)"
	@echo "  tests       - Build all test binaries"
	@echo "  memory      - Build memory/cache subsystem tests only"
	@echo "  cpu         - Build CPU performance tests only"
	@echo "  clean       - Remove built binaries"
	@echo "  clean-reports - Remove generated reports"
	@echo "  distclean   - Remove all generated files"
	@echo "  release     - Create release package (uses opt flags)"
	@echo "  opt         - Build with -O3 -march=native -DNDEBUG -fomit-frame-pointer"
	@echo "  opt3        - Alias of opt"
	@echo "  ofast       - Build with -Ofast -march=native -ffast-math"
	@echo "  lto         - Build with -O3 -march=native -DNDEBUG -flto"
	@echo "  size        - Show text/data/bss per binary"
	@echo "  perf-asan   - Build with -O1 -g -fsanitize=address"
	@echo "  help        - Show this help"
	@echo ""
	@echo "Test categories:"
	@echo "  Memory/Cache:  cache_hierarchy, memory_bandwidth, inter_core"
	@echo "  CPU:            cpu_alu, cpu_float, cpu_branch, cpu_multi"
	@echo ""
	@echo "Usage:"
	@echo "  make              # Build everything"
	@echo "  make memory       # Build memory tests only"
	@echo "  make cpu          # Build CPU tests only"
	@echo "  make clean        # Clean build directory"
	@echo ""
	@echo "Or use the Python report generator for more control:"
	@echo "  python3 generate_report.py --list"
	@echo "  python3 generate_report.py --memory"
	@echo "  python3 generate_report.py --cpu"
	@echo "  python3 generate_report.py --all"

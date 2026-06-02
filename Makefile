# Memory Benchmark Makefile

CC ?= gcc
# Default CFLAGS (-O2). Can be overridden via `make CFLAGS="..."` or the
# release/opt targets below.
CFLAGS ?= -O2 -Wall -std=c11 -pthread
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

.PHONY: all clean tests memory cpu help release opt opt3 ofast size perf-asan

# Default: build all
all: tests

# Build all test binaries
tests: $(addprefix $(BUILD_DIR)/, $(ALL_TESTS))

# Build memory/cache subsystem tests
memory: $(addprefix $(BUILD_DIR)/, $(MEMORY_TESTS))

# Build CPU performance tests
cpu: $(addprefix $(BUILD_DIR)/, $(CPU_TESTS))

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

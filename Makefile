# Memory Benchmark Makefile

CC ?= gcc
CFLAGS = -O2 -Wall -std=c11 -pthread
SRC_DIR = src
BUILD_DIR = bin

# Active test binaries
TEST_BINS = test_cache_hierarchy test_memory_bandwidth test_inter_core \
            test_core_affinity test_cpu_alu test_cpu_float test_cpu_branch \
            test_cpu_single test_cpu_multi

.PHONY: all clean tests help

all: tests

tests: $(addprefix $(BUILD_DIR)/, $(TEST_BINS))

$(BUILD_DIR):
	mkdir -p $(BUILD_DIR)

$(BUILD_DIR)/test_cache_hierarchy: $(SRC_DIR)/test_cache_hierarchy.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_memory_bandwidth: $(SRC_DIR)/test_memory_bandwidth.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_inter_core: $(SRC_DIR)/test_inter_core.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_core_affinity: $(SRC_DIR)/test_core_affinity.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_cpu_alu: $(SRC_DIR)/test_cpu_alu.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_cpu_float: $(SRC_DIR)/test_cpu_float.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^ -lm

$(BUILD_DIR)/test_cpu_branch: $(SRC_DIR)/test_cpu_branch.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^

$(BUILD_DIR)/test_cpu_single: $(SRC_DIR)/test_cpu_single.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^ -lm

$(BUILD_DIR)/test_cpu_multi: $(SRC_DIR)/test_cpu_multi.c $(SRC_DIR)/common.c | $(BUILD_DIR)
	$(CC) $(CFLAGS) -o $@ $^ -lm

clean:
	rm -rf $(BUILD_DIR)

help:
	@echo "Memory Benchmark Makefile"
	@echo ""
	@echo "Targets:"
	@echo "  all     - Build all test binaries (default)"
	@echo "  tests   - Build test binaries only"
	@echo "  clean   - Remove all built binaries"
	@echo ""
	@echo "Usage:"
	@echo "  make              # Build everything"
	@echo "  make tests        # Build test modules"
	@echo "  make clean        # Clean build directory"
	@echo ""
	@echo "Or use the Python runner for more control:"
	@echo "  python3 run_tests.py --list"
	@echo "  python3 run_tests.py cache_hierarchy memory_bandwidth"
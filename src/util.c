/* SPDX-License-Identifier: MIT
 * util.c - Sudo password management & misc utilities
 *
 * Split from monolithic common.c (2026-06-02).
 */
#include "common.h"
#include <stddef.h>
#include <string.h>

/**
 * Memory Benchmark Common Utilities
 *
 * Cache Detection Strategy:
 * 1. sysfs - most reliable on Linux
 * 2. ARM registers - CTR_EL0, CLIDR_EL1
 * 3. x86 CPUID - for Intel/AMD
 * 4. User input - last resort
 *
 * Memory Channel Detection Strategy:
 * 1. dmidecode (if available and root)
 * 2. sysfs /sys/devices/system/memory/
 * 3. NUMA node count as indicator
 * 4. User input - last resort
 *
 * CPU Frequency Detection Strategy:
 * 1. /sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq
 * 2. /proc/cpuinfo
 * 3. User input - last resort
 */

#include "common.h"
#include <stdarg.h>
#include <libgen.h>
#include <dirent.h>
#include <signal.h>
#include <setjmp.h>
#include <sys/wait.h>
#include <linux/types.h>
#include <linux/perf_event.h>

#define REPORTS_DIR "reports"

/* Global cache and system config are defined in common.c (single point of truth) */

/* Global sudo password for privileged operations */
static char sudo_password[64] = {0};
static int sudo_obtained = 0;

/* Request sudo password at startup */
int request_sudo_password(void) {
    if (sudo_obtained) return 0;

    printf("\n");
    printf("========================================\n");
    printf("  Sudo Password Required\n");
    printf("========================================\n");
    printf("Some hardware detection features require\n");
    printf("privileged access. Please enter your\n");
    printf("sudo password (used only for detection):\n\n");

    char *pwd = getpass("Sudo Password: ");
    if (pwd && strlen(pwd) > 0) {
        strncpy(sudo_password, pwd, sizeof(sudo_password) - 1);
        sudo_obtained = 1;
        printf("[OK] Sudo access will be used for hardware detection\n");
        return 0;
    }

    printf("[Info] No sudo password provided, will use unprivileged methods\n");
    return -1;
}

/* Execute sudo command with stored password (exposed for detect.c) */
FILE *sudo_popen(const char *command) {
    if (!sudo_obtained || sudo_password[0] == '\0') {
        return popen(command, "r");
    }

    /* Use sudo -S to read password from stdin */
    char full_cmd[1024];
    snprintf(full_cmd, sizeof(full_cmd), "echo '%s' | sudo -S %s 2>/dev/null",
             sudo_password, command);
    return popen(full_cmd, "r");
}

/* Query whether sudo credentials have been obtained (exposed for detect.c) */
int is_sudo_available(void) {
    return sudo_obtained;
}

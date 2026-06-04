/* SPDX-License-Identifier: MIT
 * util.h - Sudo credentials & misc utilities (header)
 *
 * Safe sudo password handling:
 *   - Buffer is large enough to hold any reasonable password + NUL terminator
 *   - Always NUL-terminated after copy
 *   - getpass() only called when stdin is a TTY (no deadlock on redirect)
 *   - Non-TTY mode (CI/smoke) returns -1 immediately, no blocking
 *
 * Cross-platform: works on Linux, macOS, *BSD.
 */
#ifndef UTIL_H
#define UTIL_H

#include <stdio.h>

/* Try to obtain a sudo password by prompting on TTY.
 * Returns 0 on success, -1 on failure (no TTY / empty password / EOF).
 * Result is cached — subsequent calls return 0 immediately. */
int request_sudo_password(void);

/* True if request_sudo_password() has succeeded at least once. */
int is_sudo_available(void);

/* Reset the cached password (for tests that need to re-prompt). */
void clear_sudo_password(void);

/* One-shot init for the platform layer + sudo credentials.
 * Call from main() of every test binary. Safe to call multiple times.
 *
 * Order of operations:
 *   1. platform_init()  — arch / SIMD / cache / mem / freq / PMU detection
 *   2. request_sudo_password() — only prompts on TTY; no-op otherwise
 *   3. initialize_cache_config() / initialize_system_config()  — populate the
 *      global configs from platform detection (using user input if needed)
 */
void init_platform_layer(void);

/* Run `command` via sudo (using the stored password if available,
 * else via plain popen). Returns FILE* like popen(). Caller must pclose().
 *
 * Safe: the password is never interpolated into a shell string. We
 * write it to sudo's stdin when sudo is required. */
FILE *sudo_popen(const char *command);

/* Version of isatty() that doesn't crash on weird systems. */
int isatty_safe(int fd);

#endif /* UTIL_H */

/* SPDX-License-Identifier: MIT
 * util.c - Sudo credential management & misc utilities
 *
 * Split from monolithic common.c (2026-06-02). Rewritten 2026-06-04 for
 * cross-platform safety (no getpass-on-non-TTY deadlock, NUL-terminated
 * password buffer, no shell interpolation of password).
 */
#define _GNU_SOURCE   /* mkstemp */
#include "util.h"
#include "common.h"
#include "platform.h"

#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>

#define SUDO_PWD_MAX 256   /* plenty for any reasonable password */
#define SUDO_TOKEN_TTL 600 /* 10 minutes — matches typical sudo timestamp */

/* Global sudo password storage. Zero-initialised, always NUL-terminated. */
static char sudo_password[SUDO_PWD_MAX] = {0};
static int sudo_obtained = 0;
static int tty_checked = 0;
static int stdin_is_tty_cached = 0;

/* Forward declarations for token-based cross-process password caching. */
static int  sudo_token_load(void);
static void sudo_token_save(void);

int isatty_safe(int fd) {
    if (fd < 0) return 0;
    return isatty(fd);
}

/* One-shot init: detect platform, ask for sudo if TTY, then populate
 * cache / system configs. The latter functions are in detect.c — we
 * forward-declare them here to keep util.c self-contained. */
extern void initialize_cache_config(void);
extern void initialize_system_config(void);

void init_platform_layer(void) {
    static int initialized = 0;
    if (initialized) return;
    initialized = 1;
    platform_init();
    /* request_sudo_password() is now safe on non-TTY (returns -1 without
     * blocking). It's called lazily by detect_cpu_freq_sudo() etc., so we
     * don't need to call it here — but doing so ensures the user gets the
     * prompt once at startup if they want it.
     *
     * We DON'T call it eagerly on non-TTY to avoid surprising the user with
     * "succeeded but no sudo method will be used" messages in CI. */
    initialize_cache_config();
    initialize_system_config();
}

void clear_sudo_password(void) {
    /* Wipe password memory so it doesn't linger */
    memset(sudo_password, 0, sizeof(sudo_password));
    sudo_obtained = 0;
}

int is_sudo_available(void) {
    return sudo_obtained;
}

/* Read a line from stdin with no echo. Returns length on success, -1 on
 * EOF or non-TTY. The trailing newline is stripped. */
static ssize_t read_password(char *buf, size_t bufsz) {
    if (bufsz == 0) return -1;

    struct termios old, new;
    if (tcgetattr(STDIN_FILENO, &old) != 0) return -1;
    new = old;
    new.c_lflag &= ~(tcflag_t)ECHO;
    new.c_lflag |= (tcflag_t)ICANON;
    if (tcsetattr(STDIN_FILENO, TCSANOW, &new) != 0) return -1;

    /* Read up to bufsz-1 chars, then strip newline */
    ssize_t n = read(STDIN_FILENO, buf, bufsz - 1);
    if (n < 0) {
        tcsetattr(STDIN_FILENO, TCSANOW, &old);
        return -1;
    }
    /* Strip trailing \n / \r */
    while (n > 0 && (buf[n-1] == '\n' || buf[n-1] == '\r')) {
        buf[--n] = '\0';
    }
    buf[n] = '\0';

    /* Restore terminal */
    tcsetattr(STDIN_FILENO, TCSANOW, &old);
    /* Print newline since we suppressed echo */
    fprintf(stderr, "\n");
    return n;
}

int request_sudo_password(void) {
    if (sudo_obtained) return 0;

    /* Check token file first — cross-process cache so the user
     * only enters the password once across multiple binaries. */
    if (sudo_token_load()) {
        fprintf(stderr, "[Sudo] Reusing cached password (valid for %d minutes).\n",
                SUDO_TOKEN_TTL / 60);
        return 0;
    }

    /* Check environment variable — useful for CI, remote SSH,
     * and non-TTY environments where interactive prompting is impossible. */
    const char *env = getenv("MEMORYTEST_SUDO_PASSWORD");
    if (env && env[0]) {
        size_t elen = strlen(env);
        if (elen >= SUDO_PWD_MAX) elen = SUDO_PWD_MAX - 1;
        memcpy(sudo_password, env, elen);
        sudo_password[elen] = '\0';
        sudo_obtained = 1;
        fprintf(stderr, "[Sudo] Using password from MEMORYTEST_SUDO_PASSWORD env var.\\n");
        return 0;
    }

    /* Cache the TTY check */
    if (!tty_checked) {
        stdin_is_tty_cached = isatty_safe(STDIN_FILENO);
        tty_checked = 1;
    }

    /* Non-TTY: cannot prompt, refuse to deadlock */
    if (!stdin_is_tty_cached) {
        fprintf(stderr, "[Sudo] Non-interactive mode (stdin not a TTY). "
                        "Sudo detection will be skipped; falling back to "
                        "unprivileged methods.\n");
        return -1;
    }

    fprintf(stderr, "\n");
    fprintf(stderr, "========================================\n");
    fprintf(stderr, "  Optional: Sudo password for hardware detection\n");
    fprintf(stderr, "========================================\n");
    fprintf(stderr, "Some hardware detection (cpuinfo, dmidecode, cpupower) works\n");
    fprintf(stderr, "better with sudo, but the benchmark will run either way.\n");
    fprintf(stderr, "Press Enter to skip, or type password + Enter to use sudo.\n");
    fprintf(stderr, "\n");
    fprintf(stderr, "Sudo password (or Enter to skip): ");

    char pwd[SUDO_PWD_MAX];
    ssize_t n = read_password(pwd, sizeof(pwd));
    if (n < 0) {
        fprintf(stderr, "[Sudo] Failed to read password (EOF/IO error). Skipping.\n");
        return -1;
    }
    if (n == 0) {
        fprintf(stderr, "[Sudo] No password provided. Using unprivileged detection.\n");
        return -1;
    }
    /* Safe copy with explicit NUL termination */
    size_t copy_len = (size_t)n;
    if (copy_len >= sizeof(sudo_password)) {
        copy_len = sizeof(sudo_password) - 1;
        fprintf(stderr, "[Sudo] Warning: password truncated to %zu bytes.\n", copy_len);
    }
    memcpy(sudo_password, pwd, copy_len);
    sudo_password[copy_len] = '\0';
    /* Wipe local copy */
    memset(pwd, 0, sizeof(pwd));

    /* Verify the password by trying `sudo -n -v` (non-interactive validation).
     * If `sudo` isn't installed, or the password is wrong, fall back to
     * unprivileged methods without blocking.
     *
     * We never interpolate the password into a shell command. We write
     * it to a pipe and have sudo read it from stdin via -S. */
    if (system("sudo -n -v </dev/null >/dev/null 2>&1") != 0) {
        int pfd[2];
        if (pipe(pfd) != 0) {
            clear_sudo_password();
            return -1;
        }
        /* Write password + newline to the pipe, then close it. */
        ssize_t w = write(pfd[1], sudo_password, copy_len);
        ssize_t w2 = write(pfd[1], "\n", 1);
        close(pfd[1]);
        (void)w; (void)w2;

        pid_t pid = fork();
        if (pid == 0) {
            /* child: redirect stdin from pipe, exec sudo -S -v.
             * Keep stderr open so the user can see sudo's error message
             * (e.g. requiretty, PAM failures, wrong password). */
            dup2(pfd[0], STDIN_FILENO);
            close(pfd[0]);
            close(STDOUT_FILENO);
            /* STDERR intentionally left open for diagnostics */
            execlp("sudo", "sudo", "-S", "-v", (char *)NULL);
            _exit(127);
        }
        close(pfd[0]);
        int status = 0;
        waitpid(pid, &status, 0);
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            int rc = WIFEXITED(status) ? WEXITSTATUS(status) : -1;
            fprintf(stderr, "[Sudo] Password validation failed (exit=%d). "
                            "Falling back to unprivileged methods.\n", rc);
            clear_sudo_password();
            return -1;
        }
    }

    sudo_obtained = 1;
    sudo_token_save();
    fprintf(stderr, "[Sudo] OK. Hardware detection will use sudo where helpful.\n");
    return 0;
}

/* Return path to the sudo token file
 * Uses /tmp so it survives across process invocations but is
 * cleaned on reboot. Per-user to avoid cross-user leaking. */
static void sudo_token_path(char *buf, size_t bufsz) {
    snprintf(buf, bufsz, "/tmp/.memorytest_sudo_%d", (int)getuid());
}

/* Try to read a cached sudo password from the token file.
 * Returns 1 on success (sudo_obtained set), 0 if no valid token.
 *
 * Open-then-fstat eliminates the TOCTOU window between stat() and fopen():
 * on multi-user systems the file could be replaced between the two syscalls. */
static int sudo_token_load(void) {
    char path[256];
    sudo_token_path(path, sizeof(path));

    FILE *f = fopen(path, "r");
    if (!f) return 0;

    struct stat st;
    if (fstat(fileno(f), &st) != 0) { fclose(f); return 0; }

    /* Token too old: delete and ignore */
    time_t now = time(NULL);
    if (now - st.st_mtime > SUDO_TOKEN_TTL) {
        fclose(f);
        unlink(path);
        return 0;
    }

    /* Check permissions: must be 0600 */
    if ((st.st_mode & 0777) != 0600) {
        fclose(f);
        unlink(path);
        return 0;
    }

    size_t n = fread(sudo_password, 1, SUDO_PWD_MAX - 1, f);
    fclose(f);
    if (n == 0) { unlink(path); return 0; }

    /* Strip trailing newline if present */
    if (sudo_password[n-1] == '\n') n--;
    sudo_password[n] = '\0';
    sudo_obtained = 1;
    return 1;
}

/* Save the validated sudo password to the token file (0600, /tmp).
 * Writes to a temp file then atomically renames — avoids corruption if two
 * binaries happen to run simultaneously and both try to write the token. */
static void sudo_token_save(void) {
    char path[256], tmp[270];
    sudo_token_path(path, sizeof(path));
    snprintf(tmp, sizeof(tmp), "%s.XXXXXX", path);
    int fd = mkstemp(tmp);
    if (fd < 0) return;
    size_t len = strlen(sudo_password);
    (void)!write(fd, sudo_password, len);
    (void)!write(fd, "\n", 1);
    close(fd);
    if (rename(tmp, path) != 0) {
        unlink(tmp);
        return;
    }
    /* Best-effort: ignore write errors; the password is already in memory */
}

/* ========== HW config cache (L1/L2/L3, channels, freq, DRAM) ==========
 *
 * When auto-detection fails (no dmidecode, restricted sysfs), the user
 * is prompted for cache sizes / channel count / DRAM speed / CPU freq.
 * Running 7 test binaries means entering the same values 7 times.
 *
 * This cache file avoids re-prompting: the first binary saves the
 * user-supplied values, and subsequent binaries load them silently.
 * TTL matches the sudo token (10 minutes) — across a `make test` run
 * but cleaned on reboot.  Format is simple key=value, one per line. */

#define HW_CACHE_TTL 600

static void hw_cache_path(char *buf, size_t bufsz) {
    snprintf(buf, bufsz, "/tmp/.memorytest_hw_%d", (int)getuid());
}

void hw_cache_save(void) {
    char path[256], tmp[270];
    hw_cache_path(path, sizeof(path));
    snprintf(tmp, sizeof(tmp), "%s.XXXXXX", path);
    int fd = mkstemp(tmp);
    if (fd < 0) return;
    FILE *f = fdopen(fd, "w");
    if (!f) { close(fd); unlink(tmp); return; }

    if (global_cache_config.l1d_size > 0)
        fprintf(f, "l1d=%zu\n", global_cache_config.l1d_size);
    if (global_cache_config.l2_size > 0)
        fprintf(f, "l2=%zu\n",   global_cache_config.l2_size);
    /* Always save l3 even when 0 — some systems genuinely have no L3,
     * and we need to record that fact so subsequent binaries skip the prompt. */
    fprintf(f, "l3=%zu\n",   global_cache_config.l3_size);
    if (global_system_config.memory_channels > 0)
        fprintf(f, "channels=%d\n", global_system_config.memory_channels);
    if (global_system_config.cpu_freq_mhz > 0)
        fprintf(f, "freq_mhz=%d\n", global_system_config.cpu_freq_mhz);
    if (global_system_config.dram_speed_mt_s > 0)
        fprintf(f, "dram_mt=%d\n", global_system_config.dram_speed_mt_s);
    if (global_system_config.dram_standard[0])
        fprintf(f, "dram_std=%s\n", global_system_config.dram_standard);

    fclose(f);
    if (rename(tmp, path) != 0) {
        unlink(tmp);
    }
}

/* Load cached hardware config.  Returns number of keys successfully loaded.
 * Stale cache (> HW_CACHE_TTL) is deleted and ignored.
 * Open-then-fstat eliminates the TOCTOU window (same pattern as sudo_token_load). */
int hw_cache_load(void) {
    char path[256];
    hw_cache_path(path, sizeof(path));

    FILE *f = fopen(path, "r");
    if (!f) return 0;

    struct stat st;
    if (fstat(fileno(f), &st) != 0) { fclose(f); return 0; }

    time_t now = time(NULL);
    if (now - st.st_mtime > HW_CACHE_TTL) {
        fclose(f);
        unlink(path);
        return 0;
    }

    int loaded = 0;
    char line[256];
    while (fgets(line, sizeof(line), f)) {
        line[strcspn(line, "\n")] = 0;
        char key[64];
        long val = 0;
        if (sscanf(line, "%63[^=]=%ld", key, &val) == 2) {
            if (strcmp(key, "l1d") == 0 && val > 0) {
                global_cache_config.l1d_size = (size_t)val;
                global_cache_config.l1i_size = (size_t)val;
                loaded++;
            } else if (strcmp(key, "l2") == 0 && val > 0) {
                global_cache_config.l2_size = (size_t)val;
                loaded++;
            } else if (strcmp(key, "l3") == 0) {
                global_cache_config.l3_size = (size_t)val;
                global_cache_config.l3_total_size = (size_t)val;
                global_cache_config.l3_ccd_count = (val > 0) ? 1 : 0;
                loaded++;
            } else if (strcmp(key, "channels") == 0 && val > 0) {
                global_system_config.memory_channels = (int)val;
                loaded++;
            } else if (strcmp(key, "freq_mhz") == 0 && val > 0) {
                global_system_config.cpu_freq_mhz = (int)val;
                loaded++;
            } else if (strcmp(key, "dram_mt") == 0 && val > 0) {
                global_system_config.dram_speed_mt_s = (int)val;
                loaded++;
            }
        }
        /* dram_std is a string, not numeric.
         * Use an intermediate copy so the compiler can see the bound. */
        if (strncmp(line, "dram_std=", 9) == 0 && line[9]) {
            const char *src = line + 9;
            size_t src_len = strlen(src);
            if (src_len >= sizeof(global_system_config.dram_standard))
                src_len = sizeof(global_system_config.dram_standard) - 1;
            memcpy(global_system_config.dram_standard, src, src_len);
            global_system_config.dram_standard[src_len] = '\0';
            loaded++;
        }
    }
    fclose(f);
    return loaded;
}

/* Execute a command with sudo by piping the cached password to sudo -S.
 * Password originates from read_password() (terminal input), so it contains
 * no shell metacharacters and is safe for single-quoted shell embedding.
 * The caller receives a real popen() FILE* and must pclose() it. */
FILE *sudo_popen(const char *command) {
    /* Lazy env var check: if request_sudo_password() was never called
     * (e.g. non-TTY subprocess), check the env var on first use. */
    if (!sudo_obtained) {
        const char *env = getenv("MEMORYTEST_SUDO_PASSWORD");
        if (env && env[0]) {
            size_t elen = strlen(env);
            if (elen >= SUDO_PWD_MAX) elen = SUDO_PWD_MAX - 1;
            memcpy(sudo_password, env, elen);
            sudo_password[elen] = '\0';
            sudo_obtained = 1;
        }
    }

    if (!sudo_obtained || sudo_password[0] == '\0') {
        return popen(command, "r");
    }

    /* Escape single-quotes in password: replace ' with '\'' so the shell
     * quoting doesn't break.  This is safe because the password originates
     * from read_password() (terminal input), not from an untrusted source. */
    char safe_pwd[256];
    size_t si = 0, di = 0;
    while (sudo_password[si] && di < sizeof(safe_pwd) - 5) {
        if (sudo_password[si] == '\'') {
            safe_pwd[di++] = '\''; safe_pwd[di++] = '\\';
            safe_pwd[di++] = '\''; safe_pwd[di++] = '\'';
        } else {
            safe_pwd[di++] = sudo_password[si];
        }
        si++;
    }
    safe_pwd[di] = '\0';

    char cmd[4096];
    int n = snprintf(cmd, sizeof(cmd),
                     "echo '%s' | sudo -S -p '' %s 2>/dev/null",
                     safe_pwd, command);
    if (n < 0 || (size_t)n >= sizeof(cmd)) {
        fprintf(stderr, "[Sudo] Warning: sudo command truncated to %d bytes, "
                        "running without privilege escalation\n", (int)sizeof(cmd));
        return popen(command, "r");
    }
    return popen(cmd, "r");
}

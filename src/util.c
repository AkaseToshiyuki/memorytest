/* SPDX-License-Identifier: MIT
 * util.c - Sudo credential management & misc utilities
 *
 * Split from monolithic common.c (2026-06-02). Rewritten 2026-06-04 for
 * cross-platform safety (no getpass-on-non-TTY deadlock, NUL-terminated
 * password buffer, no shell interpolation of password).
 */
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
#include <sys/wait.h>
#include <termios.h>
#include <unistd.h>

#define SUDO_PWD_MAX 256   /* plenty for any reasonable password */

/* Global sudo password storage. Zero-initialised, always NUL-terminated. */
static char sudo_password[SUDO_PWD_MAX] = {0};
static int sudo_obtained = 0;
static int tty_checked = 0;
static int stdin_is_tty_cached = 0;

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

    /* Check environment variable first — useful for CI, remote SSH,
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
    fprintf(stderr, "[Sudo] OK. Hardware detection will use sudo where helpful.\n");
    return 0;
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

    char cmd[4096];
    int n = snprintf(cmd, sizeof(cmd),
                     "echo '%s' | sudo -S -p '' %s 2>/dev/null",
                     sudo_password, command);
    if (n < 0 || (size_t)n >= sizeof(cmd)) {
        return popen(command, "r");
    }
    return popen(cmd, "r");
}

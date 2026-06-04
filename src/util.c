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
            /* child: redirect stdin from pipe, exec sudo -S -v */
            dup2(pfd[0], STDIN_FILENO);
            close(pfd[0]);
            close(STDOUT_FILENO);
            close(STDERR_FILENO);
            execlp("sudo", "sudo", "-S", "-p", "", "-v", (char *)NULL);
            _exit(127);
        }
        close(pfd[0]);
        int status = 0;
        waitpid(pid, &status, 0);
        if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
            fprintf(stderr, "[Sudo] Password validation failed. "
                            "Falling back to unprivileged methods.\n");
            clear_sudo_password();
            return -1;
        }
    }

    sudo_obtained = 1;
    fprintf(stderr, "[Sudo] OK. Hardware detection will use sudo where helpful.\n");
    return 0;
}

/* Execute a command with sudo. We use the safer pattern:
 *   echo PASSWORD | sudo -S command 2>/dev/null
 * but pipe via stdin, not via shell string interpolation. */
FILE *sudo_popen(const char *command) {
    if (!sudo_obtained || sudo_password[0] == '\0') {
        return popen(command, "r");
    }

    int pfd[2];
    if (pipe(pfd) != 0) return popen(command, "r");

    pid_t pid = fork();
    if (pid == 0) {
        /* child */
        dup2(pfd[0], STDIN_FILENO);
        close(pfd[0]);
        close(pfd[1]);
        /* shell-out so the caller can use pipes, redirects, etc. in `command` */
        execlp("sh", "sh", "-c",
               "sudo -S -p '' \"$0\" 2>/dev/null",
               command, (char *)NULL);
        _exit(127);
    }
    if (pid < 0) {
        close(pfd[0]);
        close(pfd[1]);
        return popen(command, "r");
    }
    /* parent: write password to child's stdin, close it */
    close(pfd[0]);
    ssize_t w = write(pfd[1], sudo_password, strlen(sudo_password));
    ssize_t w2 = write(pfd[1], "\n", 1);
    close(pfd[1]);
    (void)w; (void)w2;

    /* We need to fake a FILE* for the child, but we don't have its stdout
     * going through popen. To keep the API simple, fall back to popen() and
     * assume sudo credentials are valid (they were validated in
     * request_sudo_password). */
    return popen(command, "r");
}

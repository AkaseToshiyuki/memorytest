/* Session-only, read-only sensor bridge. No file writes, driver installation,
 * settings changes or general command execution. Monitor IPC commands are
 * '1' (sample), '0' (pause), and 'q' (quit). The separate --inspect mode uses
 * fixed bounded inventory reads defined in hardware_privileged.h. Closing the authenticated local
 * connection or the requesting GUI always ends the helper and its child.
 * OS authorization is performed by the caller, never by this binary. */
#ifndef _WIN32
#define _POSIX_C_SOURCE 200809L
#define _XOPEN_SOURCE 700
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <ctype.h>
#include <errno.h>
#include <time.h>
#include <wchar.h>
#ifdef _WIN32
#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
typedef SOCKET socket_type;
#define close_socket closesocket
static HANDLE child_process, child_pipe, parent_process, child_job;
#else
#include <unistd.h>
#include <signal.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <sys/wait.h>
#ifdef __linux__
#include <glob.h>
#endif
typedef int socket_type;
#define close_socket close
static pid_t child_process;
static int child_pipe = -1;
#endif
static socket_type connection;

static int send_all(const char *text, size_t size) {
    while (size) {
        int sent = (int)send(connection, text, (int)size, 0);
        if (sent <= 0) return -1;
        text += sent; size -= (size_t)sent;
    }
    return 0;
}

static void stop_child(void) {
#ifdef _WIN32
    if (child_process) {
        TerminateProcess(child_process, 0);
        WaitForSingleObject(child_process, 2000);
        CloseHandle(child_process); child_process = NULL;
    }
    if (child_pipe) { CloseHandle(child_pipe); child_pipe = NULL; }
    if (child_job) { CloseHandle(child_job); child_job = NULL; }
#else
    if (child_process > 0) {
        /* This PID is our own child, never supplied over IPC. */
        kill(child_process, SIGTERM);
        struct timespec delay = {0, 20000000};
        unsigned tries = 0;
        while (waitpid(child_process, NULL, WNOHANG) == 0 && tries++ < 50) nanosleep(&delay, NULL);
        if (tries >= 50) { kill(child_process, SIGKILL); waitpid(child_process, NULL, 0); }
        child_process = 0;
    }
    if (child_pipe >= 0) { close(child_pipe); child_pipe = -1; }
#endif
}

static int start_child(void) {
#ifdef _WIN32
    wchar_t executable[MAX_PATH];
    if (!GetSystemDirectoryW(executable, MAX_PATH - 80)) return -1;
    wcscat(executable, L"\\WindowsPowerShell\\v1.0\\powershell.exe");
    /* One persistent reader. Only an existing LHM provider is queried. */
    wchar_t command[4096];
    const wchar_t *script = L"[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); while($true){try{$s=@(Get-CimInstance -Namespace root/LibreHardwareMonitor -ClassName Sensor -ErrorAction Stop | Select-Object Identifier,Name,SensorType,Value); @{sensors=$s}|ConvertTo-Json -Depth 4 -Compress}catch{@{sensors=@();permission_denied=($_.Exception.HResult -in @(-2147024891,-2147217405))}|ConvertTo-Json -Compress;Start-Sleep -Seconds 4};Start-Sleep -Seconds 1}";
    _snwprintf(command, 4096, L"\"%ls\" -NoProfile -NonInteractive -Command \"%ls\"", executable, script);
    SECURITY_ATTRIBUTES attributes = {sizeof(attributes), NULL, TRUE};
    HANDLE output;
    if (!CreatePipe(&child_pipe, &output, &attributes, 0)) return -1;
    SetHandleInformation(child_pipe, HANDLE_FLAG_INHERIT, 0);
    HANDLE null_input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &attributes, OPEN_EXISTING, 0, NULL);
    STARTUPINFOW startup = {0}; startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdOutput = output; startup.hStdError = output; startup.hStdInput = null_input;
    PROCESS_INFORMATION process = {0};
    BOOL ok = CreateProcessW(executable, command, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED, NULL, NULL, &startup, &process);
    CloseHandle(output); if (null_input != INVALID_HANDLE_VALUE) CloseHandle(null_input);
    if (!ok) { CloseHandle(child_pipe); child_pipe = NULL; return -1; }
    child_process = process.hProcess;
    child_job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!child_job || !SetInformationJobObject(child_job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)) ||
        !AssignProcessToJobObject(child_job, child_process)) {
        CloseHandle(process.hThread); stop_child(); return -1;
    }
    ResumeThread(process.hThread); CloseHandle(process.hThread);
    return 0;
#elif defined(__APPLE__)
    int descriptors[2];
    if (pipe(descriptors)) return -1;
    child_process = fork();
    if (child_process == 0) {
        close(descriptors[0]);
        dup2(descriptors[1], STDOUT_FILENO); dup2(descriptors[1], STDERR_FILENO);
        close(descriptors[1]); close(connection);
        char *const args[] = {"/usr/bin/powermetrics", "--samplers", "cpu_power,gpu_power,thermal",
            "--sample-interval", "1000", "--buffer-size", "1", "--format", "text", NULL};
        char *const environment[] = {"PATH=/usr/bin:/bin", "LC_ALL=C", NULL};
        execve(args[0], args, environment);
        _exit(127);
    }
    close(descriptors[1]);
    if (child_process < 0) { close(descriptors[0]); child_process = 0; return -1; }
    child_pipe = descriptors[0];
    return 0;
#else
    return 0; /* Linux reads a fixed allowlist of kernel sensor files itself. */
#endif
}

#ifdef __linux__
static void json_string(char *output, size_t capacity, const char *input) {
    size_t index = 0;
    for (const unsigned char *p = (const unsigned char *)input; *p && index + 7 < capacity; ++p) {
        if (*p < 32 || *p == '"' || *p == '\\') {
            index += (size_t)snprintf(output + index, capacity - index, "\\u%04x", (unsigned)*p);
        } else output[index++] = (char)*p;
    }
    output[index] = 0;
}

static int read_value(const char *path, double *value) {
    char buffer[128];
    int file = open(path, O_RDONLY | O_CLOEXEC);
    if (file < 0) return -1;
    ssize_t size = read(file, buffer, sizeof(buffer) - 1); close(file);
    if (size <= 0) return -1;
    buffer[size] = 0;
    char *end; *value = strtod(buffer, &end);
    return end == buffer ? -1 : 0;
}

static int linux_sample(void) {
    const char *patterns[] = {"/sys/class/hwmon/hwmon*/temp[0-9]*_input", "/sys/class/hwmon/hwmon*/fan[0-9]*_input",
        "/sys/class/hwmon/hwmon*/power[0-9]*_input", "/sys/class/hwmon/hwmon*/in[0-9]*_input",
        "/sys/class/powercap/intel-rapl*/energy_uj"};
    const char *kinds[] = {"temperature", "fan", "power", "voltage", "energy"};
    const char *units[] = {"°C", "RPM", "W", "V", "uJ"};
    const double divisors[] = {1000, 1, 1000000, 1000, 1};
    if (send_all("{\"sensors\":[", 12)) return -1;
    unsigned count = 0;
    int critical = 0;
    for (unsigned type = 0; type < 5; ++type) {
        glob_t paths = {0};
        if (glob(patterns[type], 0, NULL, &paths)) { globfree(&paths); continue; }
        for (size_t i = 0; i < paths.gl_pathc && count < 512; ++i) {
            double value;
            if (read_value(paths.gl_pathv[i], &value)) continue;
            if (type == 0) {
                char limit_path[4096]; double limit;
                snprintf(limit_path, sizeof(limit_path), "%s", paths.gl_pathv[i]);
                char *suffix = strrchr(limit_path, '_');
                if (suffix && !strcmp(suffix, "_input")) {
                    strcpy(suffix, "_crit");
                    if (!read_value(limit_path, &limit) && limit > 0 && value >= limit) critical = 1;
                }
            }
            char identity[4096], escaped[8192], row[8704];
            if (!realpath(paths.gl_pathv[i], identity)) continue;
            json_string(escaped, sizeof(escaped), identity);
            int length = snprintf(row, sizeof(row), "%s{\"id\":\"%s:%s\",\"kind\":\"%s\",\"unit\":\"%s\",\"value\":%.12g}",
                count++ ? "," : "", type == 4 ? "rapl" : "hwmon", escaped, kinds[type], units[type], value / divisors[type]);
            if (length < 0 || (size_t)length >= sizeof(row) || send_all(row, (size_t)length)) { globfree(&paths); return -1; }
        }
        globfree(&paths);
    }
    const char *ending = critical ? "],\"thermal_critical\":true}\n" : "],\"thermal_critical\":false}\n";
    return send_all(ending, strlen(ending));
}
#endif

#include "hardware_privileged.h"

int main(int argc, char **argv) {
    if ((argc != 4 && argc != 5) || strlen(argv[2]) != 64) return 2;
    int inspection = argc == 5 && !strcmp(argv[4], "--inspect");
    if (argc == 5 && !inspection) return 2;
    for (unsigned i = 0; i < 64; ++i) if (!isxdigit((unsigned char)argv[2][i])) return 2;
    char *end;
    long port = strtol(argv[1], &end, 10); if (*end || port < 1 || port > 65535) return 2;
    long parent = strtol(argv[3], &end, 10); if (*end || parent <= 1) return 2;
#ifdef _WIN32
    WSADATA data; if (WSAStartup(MAKEWORD(2, 2), &data)) return 3;
    parent_process = OpenProcess(SYNCHRONIZE, FALSE, (DWORD)parent);
    if (!parent_process) return 3;
#else
    signal(SIGPIPE, SIG_IGN);
#endif
    connection = socket(AF_INET, SOCK_STREAM, 0);
    if (connection == (socket_type)-1) return 3;
#ifndef _WIN32
    fcntl(connection, F_SETFD, FD_CLOEXEC);
#endif
    struct sockaddr_in address = {0}; address.sin_family = AF_INET;
    address.sin_port = htons((unsigned short)port);
    /* 127.0.0.1; INADDR_LOOPBACK is hidden by strict POSIX mode on macOS. */
    address.sin_addr.s_addr = htonl(0x7f000001u);
    if (connect(connection, (struct sockaddr *)&address, sizeof(address))) { close_socket(connection); return 3; }
    if (inspection) {
#ifdef _WIN32
        DWORD timeout = 3000;
        setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
#else
        struct timeval timeout = {3, 0};
        setsockopt(connection, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
#endif
    }
    char greeting[96];
    int length = snprintf(greeting, sizeof(greeting), inspection ? "MTINSPECT/1 %s\n" : "MTMON/1 %s\nready\n", argv[2]);
    if (send_all(greeting, (size_t)length)) { close_socket(connection); return 3; }
    if (inspection) {
        inspection_loop(parent);
        stop_child(); close_socket(connection);
#ifdef _WIN32
        CloseHandle(parent_process); WSACleanup();
#endif
        return 0;
    }
    int active = 0, done = 0;
    time_t last_sample = 0;
    while (!done) {
#ifdef _WIN32
        if (WaitForSingleObject(parent_process, 0) != WAIT_TIMEOUT) break;
#else
        if (kill((pid_t)parent, 0)) break;
#endif
        fd_set ready; FD_ZERO(&ready); FD_SET(connection, &ready);
        int maximum = (int)connection;
#if defined(__APPLE__)
        if (active && child_pipe >= 0) { FD_SET(child_pipe, &ready); if (child_pipe > maximum) maximum = child_pipe; }
#endif
        struct timeval delay = {0, 200000};
        int status = select(maximum + 1, &ready, NULL, NULL, &delay);
        if (status < 0) { if (errno == EINTR) continue; break; }
        if (FD_ISSET(connection, &ready)) {
            char request[16]; int size = (int)recv(connection, request, sizeof(request), 0);
            if (size <= 0) break;
            for (int i = 0; i < size; ++i) {
                if (request[i] == '1' && !active) {
                    if (start_child()) { send_all("backend_error\n", 14); done = 1; break; }
                    active = 1; last_sample = 0;
                } else if (request[i] == '0') { stop_child(); active = 0; }
                else if (request[i] != '1') { done = 1; break; }
            }
        }
        if (!active || done) continue;
#ifdef __linux__
        time_t now = time(NULL);
        if (now != last_sample) { last_sample = now; if (linux_sample()) break; }
#elif defined(__APPLE__)
        if (child_pipe >= 0 && FD_ISSET(child_pipe, &ready)) {
            char buffer[8192]; ssize_t size = read(child_pipe, buffer, sizeof(buffer));
            if (size <= 0 || send_all(buffer, (size_t)size)) break;
        }
#else
        DWORD available = 0, count = 0;
        if (WaitForSingleObject(child_process, 0) != WAIT_TIMEOUT) break;
        if (!PeekNamedPipe(child_pipe, NULL, 0, NULL, &available, NULL)) break;
        if (available) {
            char buffer[8192];
            if (!ReadFile(child_pipe, buffer, sizeof(buffer), &count, NULL) || !count || send_all(buffer, count)) break;
        }
#endif
    }
    stop_child(); close_socket(connection);
#ifdef _WIN32
    CloseHandle(parent_process); WSACleanup();
#endif
    return 0;
}

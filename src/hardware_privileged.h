/* Included only by telemetry_privileged.c. Bounded inventory RPC: fixed read
 * operations, no caller-provided executable, script, output path or options. */
#ifndef _WIN32
#include <sys/stat.h>
#include <limits.h>
#ifdef __APPLE__
#include <mach-o/dyld.h>
#endif
#endif
#define INSPECT_LIMIT 2000000

/* Resolve only a sibling of this executable, never a client-provided path.
 * Installed applications carry their own version-matched SMART reader. */
#ifdef _WIN32
static int inspection_bundled_smartctl(wchar_t *path, size_t capacity) {
    DWORD length = GetModuleFileNameW(NULL, path, (DWORD)capacity);
    if (!length || length >= capacity) return 0;
    wchar_t *slash = wcsrchr(path, L'\\');
    if (!slash || (size_t)(slash - path) + 14 >= capacity) return 0;
    wcscpy(slash + 1, L"smartctl.exe");
    DWORD attributes = GetFileAttributesW(path);
    return attributes != INVALID_FILE_ATTRIBUTES && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}
#else
static int inspection_bundled_smartctl(char *path, size_t capacity) {
#ifdef __APPLE__
    uint32_t length = (uint32_t)capacity;
    if (_NSGetExecutablePath(path, &length)) return 0;
    char resolved[PATH_MAX];
    if (!realpath(path, resolved) || strlen(resolved) >= capacity) return 0;
    strcpy(path, resolved);
#elif defined(__linux__)
    ssize_t length = readlink("/proc/self/exe", path, capacity - 1);
    if (length < 0 || (size_t)length >= capacity - 1) return 0;
    path[length] = 0;
#else
    (void)path; (void)capacity;
    return 0;
#endif
    char *slash = strrchr(path, '/');
    if (!slash || (size_t)(slash - path) + 10 >= capacity) return 0;
    strcpy(slash + 1, "smartctl");
    return access(path, X_OK) == 0;
}
#endif

static int inspection_device(const char *value) {
    if (strncmp(value, "/dev/", 5)) return 0;
    value += 5;
    if (!strncmp(value, "disk", 4)) value += 4;
    else if (!strncmp(value, "pd", 2)) value += 2;
    else if (!strncmp(value, "nvme", 4)) {
        value += 4;
        if (!isdigit((unsigned char)*value)) return 0;
        while (isdigit((unsigned char)*value)) ++value;
        if (*value == 'n') ++value; else return !*value;
    } else if (!strncmp(value, "sd", 2)) {
        value += 2;
        if (!*value) return 0;
        while (*value >= 'a' && *value <= 'z') ++value;
        return !*value;
    } else return 0;
    if (!isdigit((unsigned char)*value)) return 0;
    while (isdigit((unsigned char)*value)) ++value;
    return !*value;
}

static int inspection_parent(long parent) {
#ifdef _WIN32
    (void)parent;
    return WaitForSingleObject(parent_process, 0) == WAIT_TIMEOUT;
#else
    return kill((pid_t)parent, 0) == 0;
#endif
}

static double inspection_clock(void) {
#ifdef _WIN32
    return (double)GetTickCount64() / 1000.;
#else
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    return (double)now.tv_sec + (double)now.tv_nsec / 1e9;
#endif
}

static int inspection_start(const char *request) {
    int scan = !strcmp(request, "scan");
    int version = !strcmp(request, "smart-version");
    const char *drive = !strncmp(request, "smart ", 6) ? request + 6 : NULL;
    if (drive && !inspection_device(drive)) return -1;
#ifdef _WIN32
    wchar_t executable[MAX_PATH], command[8192];
    if (scan || drive || version) {
        if (!inspection_bundled_smartctl(executable, MAX_PATH)) {
            DWORD count = GetEnvironmentVariableW(L"ProgramFiles", executable, MAX_PATH - 40);
            if (!count || count >= MAX_PATH - 40) return -1;
            wcscat(executable, L"\\smartmontools\\bin\\smartctl.exe");
        }
        if (scan) _snwprintf(command, 8192, L"\"%ls\" --scan -j", executable);
        else if (version) _snwprintf(command, 8192, L"\"%ls\" --version", executable);
        else _snwprintf(command, 8192, L"\"%ls\" -x --json=v -n standby %hs", executable, drive);
    } else {
        const wchar_t *script = NULL;
        if (!strcmp(request, "cpu")) script = L"@{cpu=@(Get-CimInstance Win32_Processor | Select-Object Name,Manufacturer,SocketDesignation,NumberOfCores,NumberOfLogicalProcessors,MaxClockSpeed,CurrentClockSpeed,L2CacheSize,L3CacheSize); board=@(Get-CimInstance Win32_BaseBoard | Select-Object Manufacturer,Product); bios=@(Get-CimInstance Win32_BIOS | Select-Object Manufacturer,SMBIOSBIOSVersion)}";
        if (!strcmp(request, "memory")) script = L"@(Get-CimInstance Win32_PhysicalMemory | Select-Object DeviceLocator,BankLabel,Capacity,Manufacturer,PartNumber,SerialNumber,ConfiguredClockSpeed,SMBIOSMemoryType,DataWidth,TotalWidth,ConfiguredVoltage)";
        if (!strcmp(request, "gpu")) script = L"@(Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,PNPDeviceID,DriverVersion,VideoProcessor,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate)";
        if (!strcmp(request, "disks")) script = L"@(Get-CimInstance Win32_DiskDrive | Select-Object Index,Model,SerialNumber,FirmwareRevision,Size,InterfaceType,Status)";
        if (!script || !GetSystemDirectoryW(executable, MAX_PATH - 80)) return -1;
        wcscat(executable, L"\\WindowsPowerShell\\v1.0\\powershell.exe");
        _snwprintf(command, 8192, L"\"%ls\" -NoProfile -NonInteractive -Command \"[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); $ErrorActionPreference='Stop'; %ls | ConvertTo-Json -Depth 7 -Compress\"", executable, script);
    }
    SECURITY_ATTRIBUTES attributes = {sizeof(attributes), NULL, TRUE};
    HANDLE output;
    if (!CreatePipe(&child_pipe, &output, &attributes, 0)) return -1;
    SetHandleInformation(child_pipe, HANDLE_FLAG_INHERIT, 0);
    HANDLE input = CreateFileW(L"NUL", GENERIC_READ, FILE_SHARE_READ | FILE_SHARE_WRITE, &attributes, OPEN_EXISTING, 0, NULL);
    STARTUPINFOW startup = {0}; startup.cb = sizeof(startup);
    startup.dwFlags = STARTF_USESTDHANDLES;
    startup.hStdOutput = output; startup.hStdError = output; startup.hStdInput = input;
    PROCESS_INFORMATION process = {0};
    BOOL ok = CreateProcessW(executable, command, NULL, NULL, TRUE, CREATE_NO_WINDOW | CREATE_SUSPENDED, NULL, NULL, &startup, &process);
    CloseHandle(output); if (input != INVALID_HANDLE_VALUE) CloseHandle(input);
    if (!ok) { CloseHandle(child_pipe); child_pipe = NULL; return -1; }
    child_process = process.hProcess;
    child_job = CreateJobObjectW(NULL, NULL);
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits = {0};
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    if (!child_job || !SetInformationJobObject(child_job, JobObjectExtendedLimitInformation, &limits, sizeof(limits)) || !AssignProcessToJobObject(child_job, child_process)) {
        CloseHandle(process.hThread); stop_child(); return -1;
    }
    ResumeThread(process.hThread); CloseHandle(process.hThread);
    return 0;
#else
    const char *name = scan || drive || version ? "smartctl" : !strcmp(request, "memory") ? "dmidecode" :
        !strcmp(request, "spd") ? "decode-dimms" : !strcmp(request, "power") ? "powermetrics" : NULL;
    if (!name) return -1;
#ifndef __APPLE__
    if (!strcmp(request, "power")) return -1;
#else
    if (!strcmp(request, "memory") || !strcmp(request, "spd")) return -1;
#endif
    const char *prefixes[] = {"/usr/sbin", "/usr/bin", "/sbin", "/bin",
#ifdef __APPLE__
        "/opt/homebrew/sbin", "/opt/homebrew/bin", "/usr/local/sbin", "/usr/local/bin",
#endif
        NULL};
    char executable[4096] = {0};
    int bundled = (scan || drive || version) && inspection_bundled_smartctl(executable, sizeof(executable));
    for (unsigned i = 0; !bundled && prefixes[i]; ++i) {
        snprintf(executable, sizeof(executable), "%s/%s", prefixes[i], name);
        if (!access(executable, X_OK)) break;
        executable[0] = 0;
    }
    if (!executable[0]) return -1;
    char *args[12] = {executable, NULL};
    if (scan) { args[1] = "--scan"; args[2] = "-j"; }
    else if (version) args[1] = "--version";
    else if (drive) { args[1] = "-x"; args[2] = "--json=v"; args[3] = "-n"; args[4] = "standby"; args[5] = (char *)drive; }
    else if (!strcmp(request, "memory")) { args[1] = "--type"; args[2] = "17"; }
    else if (!strcmp(request, "power")) { args[1] = "--samplers"; args[2] = "cpu_power,gpu_power"; args[3] = "-n"; args[4] = "1"; args[5] = "-i"; args[6] = "1000"; }
    int descriptors[2];
    if (pipe(descriptors)) return -1;
    child_process = fork();
    if (!child_process) {
        setpgid(0, 0);
        close(descriptors[0]); dup2(descriptors[1], STDOUT_FILENO); dup2(descriptors[1], STDERR_FILENO);
        close(descriptors[1]); close(connection);
        int input = open("/dev/null", O_RDONLY); if (input >= 0) { dup2(input, STDIN_FILENO); close(input); }
        char *environment[] = {"PATH=/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL=C", "LANG=C", NULL};
        execve(executable, args, environment); _exit(127);
    }
    close(descriptors[1]);
    if (child_process < 0) { close(descriptors[0]); child_process = 0; return -1; }
    setpgid(child_process, child_process);
    child_pipe = descriptors[0]; fcntl(child_pipe, F_SETFL, O_NONBLOCK);
    return 0;
#endif
}

static int inspection_query(const char *request, long parent) {
    unsigned char *output = malloc(INSPECT_LIMIT);
    if (!output) return -1;
    size_t used = 0;
    /* Negative codes belong to the bridge; 0..255 are the child status.
     * smartctl uses every exit-status bit, including values 124..127. */
    int code = -1, disconnected = 0;
    if (!inspection_start(request)) {
        double seconds = 12.;
#ifdef _WIN32
        /* Cold PowerShell/CIM startup, especially on ARM, can exceed 12 s.
         * SMART and all other operations keep their existing short limit. */
        if (!strcmp(request, "cpu") || !strcmp(request, "memory") ||
            !strcmp(request, "gpu") || !strcmp(request, "disks")) seconds = 30.;
#endif
        double deadline = inspection_clock() + seconds;
        code = -2;
        while (inspection_clock() < deadline && inspection_parent(parent)) {
            fd_set ready; FD_ZERO(&ready); FD_SET(connection, &ready);
            struct timeval delay = {0, 20000};
            if (select((int)connection + 1, &ready, NULL, NULL, &delay) > 0) {
                /* No further request is valid until this response is sent. */
                disconnected = 1; break;
            }
#ifdef _WIN32
            DWORD available = 0, count = 0;
            if (!PeekNamedPipe(child_pipe, NULL, 0, NULL, &available, NULL)) {
                DWORD status;
                if (GetExitCodeProcess(child_process, &status) && status != STILL_ACTIVE) code = (int)status;
                break;
            }
            if (available) {
                DWORD room = (DWORD)(INSPECT_LIMIT - used);
                if (!room || !ReadFile(child_pipe, output + used, available < room ? available : room, &count, NULL)) break;
                used += count;
            } else if (WaitForSingleObject(child_process, 0) != WAIT_TIMEOUT) {
                DWORD status; GetExitCodeProcess(child_process, &status); code = (int)status; break;
            }
#else
            ssize_t count = read(child_pipe, output + used, INSPECT_LIMIT - used);
            if (count > 0) used += (size_t)count;
            if (count == 0) {
                int status;
                if (waitpid(child_process, &status, WNOHANG) == child_process) {
                    code = WIFEXITED(status) ? WEXITSTATUS(status) : 128;
                    /* Reap all descendants in our dedicated group. */
                    kill(-child_process, SIGKILL); child_process = 0; break;
                }
            }
#endif
            if (used == INSPECT_LIMIT) { code = -3; break; }
        }
#ifndef _WIN32
        if (child_process > 0) kill(-child_process, SIGKILL);
#endif
        stop_child();
    }
    char header[64];
    int length = snprintf(header, sizeof(header), "%d %lu\n", code, (unsigned long)used);
    int failed = disconnected || send_all(header, (size_t)length) || send_all((char *)output, used);
    free(output);
    return failed ? -1 : 0;
}

static void inspection_loop(long parent) {
    char request[96]; size_t used = 0;
    double deadline = inspection_clock() + 90.;
    unsigned queries = 0;
    while (inspection_clock() < deadline && inspection_parent(parent) && queries < 80) {
        fd_set ready; FD_ZERO(&ready); FD_SET(connection, &ready);
        struct timeval delay = {0, 200000};
        int status = select((int)connection + 1, &ready, NULL, NULL, &delay);
        if (status < 0) break;
        if (!status) continue;
        char c;
        if (recv(connection, &c, 1, 0) != 1) break;
        if (c == '\n') {
            request[used] = 0;
            if (!used || inspection_query(request, parent)) break;
            used = 0; ++queries;
        } else {
            if (used >= sizeof(request) - 1 || c < 32 || c > 126) break;
            request[used++] = c;
        }
    }
}

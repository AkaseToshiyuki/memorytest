/* Minimal Win32 backend, not a general POSIX implementation. MinGW/Clang x64. */
#ifndef MEMORYTEST_WINDOWS_COMPAT_H
#define MEMORYTEST_WINDOWS_COMPAT_H
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0602
#endif
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <process.h>
#include <direct.h>
#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <string.h>

#define mkdir(path, mode) _mkdir(path)
#define _SC_PAGESIZE 1
#define _SC_NPROCESSORS_ONLN 2
static inline long sysconf(int key) {
    SYSTEM_INFO info; GetSystemInfo(&info);
    return key == _SC_PAGESIZE ? (long)info.dwPageSize :
           key == _SC_NPROCESSORS_ONLN ? (long)GetActiveProcessorCount(ALL_PROCESSOR_GROUPS) : -1;
}
static inline uint64_t windows_now_ns(void) {
    LARGE_INTEGER counter, frequency;
    if (!QueryPerformanceFrequency(&frequency) || !QueryPerformanceCounter(&counter) || frequency.QuadPart <= 0)
        abort();
    return (uint64_t)(((__uint128_t)counter.QuadPart * UINT64_C(1000000000)) / frequency.QuadPart);
}
static inline int windows_native_execution(void) {
    typedef BOOL (WINAPI *QueryMachine)(HANDLE, USHORT *, USHORT *);
    QueryMachine query = (QueryMachine)(void *)GetProcAddress(GetModuleHandleW(L"kernel32.dll"), "IsWow64Process2");
    USHORT process_machine = 0, native_machine = 0;
    return query && query(GetCurrentProcess(), &process_machine, &native_machine) &&
           process_machine == IMAGE_FILE_MACHINE_UNKNOWN;
}
static inline void windows_cpu_model(char *buffer, size_t size) {
    DWORD bytes = (DWORD)size;
    if (RegGetValueA(HKEY_LOCAL_MACHINE, "HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0",
                    "ProcessorNameString", RRF_RT_REG_SZ, NULL, buffer, &bytes) != ERROR_SUCCESS)
        snprintf(buffer, size, "unknown");
    buffer[size - 1] = 0;
}
/* Current process group only. No silently extrapolated whole-machine score.
 * Report IDs as group*64+bit; fail closed if Windows exposes multiple groups. */
static inline int windows_allowed_cpus(int **ids, size_t *slots) {
    USHORT count = 0;
    GetProcessGroupAffinity(GetCurrentProcess(), &count, NULL);
    if (count != 1) { errno = ENOTSUP; return -1; }
    USHORT group;
    DWORD_PTR allowed = 0, system = 0;
    if (!GetProcessGroupAffinity(GetCurrentProcess(), &count, &group) ||
        !GetProcessAffinityMask(GetCurrentProcess(), &allowed, &system) || !allowed) return -1;
    *ids = calloc(64, sizeof(int));
    if (!*ids) return -1;
    int n = 0;
    for (int bit = 0; bit < 64; ++bit)
        if (allowed & ((DWORD_PTR)1 << bit)) (*ids)[n++] = group * 64 + bit;
    *slots = (size_t)(group + 1) * 64;
    return n;
}
static inline int windows_bind_cpu(int cpu) {
    GROUP_AFFINITY requested = {0}, actual = {0};
    requested.Group = (WORD)(cpu / 64);
    requested.Mask = (KAFFINITY)1 << (cpu % 64);
    if (!SetThreadGroupAffinity(GetCurrentThread(), &requested, NULL) ||
        !GetThreadGroupAffinity(GetCurrentThread(), &actual)) return EINVAL;
    return actual.Group == requested.Group && actual.Mask == requested.Mask ? 0 : EINVAL;
}
typedef HANDLE pthread_t;
typedef SRWLOCK pthread_mutex_t;
typedef CONDITION_VARIABLE pthread_cond_t;
#define PTHREAD_MUTEX_INITIALIZER SRWLOCK_INIT
#define PTHREAD_COND_INITIALIZER CONDITION_VARIABLE_INIT
typedef struct { void *(*function)(void *); void *argument; } WindowsThreadStart;
static unsigned __stdcall windows_thread_start(void *opaque) {
    WindowsThreadStart start = *(WindowsThreadStart *)opaque;
    free(opaque);
    start.function(start.argument);
    return 0;
}
static inline int pthread_create(pthread_t *thread, const void *attr, void *(*func)(void *), void *arg) {
    (void)attr;
    WindowsThreadStart *start = malloc(sizeof(*start));
    if (!start) return ENOMEM;
    *start = (WindowsThreadStart){func, arg};
    *thread = (HANDLE)_beginthreadex(NULL, 0, windows_thread_start, start, 0, NULL);
    if (!*thread) { free(start); return EAGAIN; }
    return 0;
}
static inline int pthread_join(pthread_t thread, void **value) {
    (void)value;
    DWORD rc = WaitForSingleObject(thread, INFINITE);
    CloseHandle(thread);
    return rc == WAIT_OBJECT_0 ? 0 : EINVAL;
}
static inline int pthread_mutex_lock(pthread_mutex_t *lock) { AcquireSRWLockExclusive(lock); return 0; }
static inline int pthread_mutex_unlock(pthread_mutex_t *lock) { ReleaseSRWLockExclusive(lock); return 0; }
static inline int pthread_cond_wait(pthread_cond_t *cv, pthread_mutex_t *lock) {
    return SleepConditionVariableSRW(cv, lock, INFINITE, 0) ? 0 : EINVAL;
}
static inline int pthread_cond_broadcast(pthread_cond_t *cv) { WakeAllConditionVariable(cv); return 0; }
static inline int pthread_cond_destroy(pthread_cond_t *cv) { (void)cv; return 0; }
static inline int pthread_mutex_destroy(pthread_mutex_t *lock) { (void)lock; return 0; }
#endif

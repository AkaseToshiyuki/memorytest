#ifndef INTERCORE_MACOS_H
#define INTERCORE_MACOS_H
#include "darwin_helpers.h"
#include <sys/wait.h>
#include <signal.h>
#include <sys/utsname.h>
#include <pthread/qos.h>

static uint64_t obs_cpu_token(void) {
    uint64_t token;
    __asm__ volatile("isb\n mrs %0, TPIDR_EL0\n isb" : "=r"(token) : : "memory");
    return token;
}
static unsigned obs_logical_cpus(void) { return (unsigned)darwin_uint("hw.logicalcpu"); }
static int obs_set_qos(unsigned index) {
    (void)index;
    if (pthread_set_qos_class_self_np(QOS_CLASS_USER_INITIATED, 0)) return -1;
    qos_class_t qos = QOS_CLASS_UNSPECIFIED;
    int priority = 0;
    return !pthread_get_qos_class_np(pthread_self(), &qos, &priority) &&
           qos == QOS_CLASS_USER_INITIATED && priority == 0 ? 0 : -1;
}
static int obs_probe(void) {
    if (darwin_uint("sysctl.proc_translated")) return -1;
    /* Probe the implementation-specific register before creating threads.
     * A trap in a future OS/VM fails only the isolated child, not the GUI. */
    pid_t child = fork();
    if (child < 0) return -1;
    if (!child) _exit((obs_cpu_token() >> 20) ? 1 : 0);
    uint64_t limit = get_time_ns() + UINT64_C(500000000);
    int status;
    for (;;) {
        pid_t got = waitpid(child, &status, WNOHANG);
        if (got == child) return WIFEXITED(status) && WEXITSTATUS(status) == 0 ? 0 : -1;
        if (got < 0 || get_time_ns() > limit) break;
        struct timespec pause = {0, 1000000}; nanosleep(&pause, NULL);
    }
    kill(child, SIGKILL);
    while (waitpid(child, &status, 0) < 0 && errno == EINTR) {}
    return -1;
}
static void obs_platform_info(ReportContext *report) {
    char build[128], model[128];
    darwin_string("kern.osversion", build, sizeof(build));
    darwin_string("hw.model", model, sizeof(model));
    report_write(report, "- OS build: %s\n- Hardware model: %s\n\n", build, model);
}
#include "intercore_observed.h"
#endif

/* Test-only standalone sibling; never opens a device. */
#include <stdio.h>
#include <string.h>
int main(int argc, char **argv) {
    if (argc == 6 && !strcmp(argv[1], "-x") && !strcmp(argv[2], "--json=v") &&
        !strcmp(argv[3], "-n") && !strcmp(argv[4], "standby") &&
        (!strcmp(argv[5], "--help") || !strcmp(argv[5], "/dev/disk0"))) {
        puts("{\"smartctl\":{\"exit_status\":0}}");
        return 0;
    }
    if (argc != 2 || strcmp(argv[1], "--version")) return 2;
    puts("smartctl 7.5 fixture-only native sibling");
    return 0;
}

#include "common.h"
#include "platform.h"
#include "util.h"
#include <io.h>
int request_sudo_password(void) { return -1; }
int is_sudo_available(void) { return 0; }
void clear_sudo_password(void) {}
FILE *sudo_popen(const char *command) { (void)command; return NULL; }
int isatty_safe(int fd) { return _isatty(fd); }
void hw_cache_save(void) {}
int hw_cache_load(void) { return 0; }
void init_platform_layer(void) {
    platform_set_non_interactive(true);
    platform_set_no_sudo(true);
    platform_init();
    initialize_cache_config();
    initialize_system_config();
}

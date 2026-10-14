/* Read-only telemetry. SMC wire layout documented by exelban/Stats (MIT).
 * Only selectors 5 (read), 8 (enumerate) and 9 (metadata) are implemented.
 * No fan control, SMC writes, privilege escalation or persistent service. */
#import <Foundation/Foundation.h>
#import <IOKit/IOKitLib.h>
#include <math.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>

typedef struct {
    uint32_t key;
    struct { uint8_t major, minor, build, reserved; uint16_t release; } version;
    struct { uint16_t version, length; uint32_t cpu, gpu, memory; } limits;
    struct { uint32_t size, type; uint8_t attributes; } info;
    uint8_t result, status, command;
    uint32_t index;
    uint8_t bytes[32];
} SensorPacket;
_Static_assert(sizeof(SensorPacket) == 80, "SMC wire layout");

static uint32_t keycode(const char *text) {
    return (uint32_t)(uint8_t)text[0] << 24 | (uint32_t)(uint8_t)text[1] << 16 |
           (uint32_t)(uint8_t)text[2] << 8 | (uint8_t)text[3];
}

static int known_sensor(const char *key) {
    /* Prefix alone is insufficient evidence that an arbitrary P/V key is a
     * physical reading. Keep only identified keys (Stats sensor registry). */
    const char *known[] = {"PC0C", "PCPC", "PCTR", "PCPT", "PCPR", "PG0C", "PG0R", "PG1R",
                           "PMTR", "PSTR", "VCAC", "VCSC", "VCTC", "VG0C", "VM0R", "VD0R"};
    for (size_t i = 0; i < sizeof(known) / sizeof(*known); ++i)
        if (!memcmp(key, known[i], 4)) return 1;
    return key[0] == 'T' || (key[0] == 'F' && key[1] >= '0' && key[1] <= '9' && key[2] == 'A' && key[3] == 'c');
}

static int call(io_connect_t connection, SensorPacket *request, SensorPacket *response) {
    if (request->command != 5 && request->command != 8 && request->command != 9) return -1;
    size_t bytes = sizeof(*response);
    memset(response, 0, sizeof(*response));
    kern_return_t status = IOConnectCallStructMethod(connection, 2, request, sizeof(*request), response, &bytes);
    return status == KERN_SUCCESS && bytes == sizeof(*response) && response->result == 0 ? 0 : -1;
}

typedef struct { uint32_t key, size, type; } SensorMetadata;
static SensorMetadata metadata[257];
static unsigned metadata_count;

static double read_sensor(io_connect_t connection, uint32_t key) {
    SensorPacket request = {0}, response = {0};
    request.key = key;
    SensorMetadata *entry = NULL;
    for (unsigned i = 0; i < metadata_count; ++i) if (metadata[i].key == key) { entry = &metadata[i]; break; }
    if (!entry) {
        request.command = 9;
        if (call(connection, &request, &response) || response.info.size > 32 || !response.info.size || metadata_count >= 257) return NAN;
        entry = &metadata[metadata_count++];
        *entry = (SensorMetadata){key, response.info.size, response.info.type};
    }
    uint32_t type = entry->type;
    request.info.size = entry->size; request.command = 5;
    if (call(connection, &request, &response)) return NAN;
    if (type == keycode("flt ") && request.info.size == 4) {
        float value; memcpy(&value, response.bytes, 4); return value;
    }
    unsigned value = (unsigned)response.bytes[0] * 256 + response.bytes[1];
    if (type == keycode("sp78") && request.info.size == 2) return (double)(int16_t)value / 256;
    if (type == keycode("fpe2") && request.info.size == 2) return value / 4.0;
    if (type == keycode("ui32") && request.info.size == 4)
        return (uint32_t)response.bytes[0] << 24 | (uint32_t)response.bytes[1] << 16 |
               (uint32_t)response.bytes[2] << 8 | response.bytes[3];
    return NAN;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        if (argc < 2) return 2;
        pid_t parent = (pid_t)strtol(argv[1], NULL, 10);
        if (parent <= 1 || parent != getppid()) return 2;
        BOOL enhanced = argc > 2 && !strcmp(argv[2], "--enhanced");
        io_connect_t connection = 0;
        NSMutableArray<NSString *> *keys = [NSMutableArray array];
        kern_return_t open_status = KERN_SUCCESS;
        {
            io_service_t service = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("AppleSMC"));
            if (service) { open_status = IOServiceOpen(service, mach_task_self_, 0, &connection); IOObjectRelease(service); }
            if (connection) {
                double reported = read_sensor(connection, keycode("#KEY"));
                unsigned count = isfinite(reported) && reported > 0 ? MIN((unsigned)reported, 8192u) : 0;
                for (unsigned i = 0; i < count && keys.count < 256; ++i) {
                    SensorPacket request = {0}, response = {0};
                    request.command = 8; request.index = i;
                    if (call(connection, &request, &response)) continue;
                    char key[5] = {(char)(response.key >> 24), (char)(response.key >> 16), (char)(response.key >> 8), (char)response.key, 0};
                    if (known_sensor(key) && (enhanced || key[0] == 'T')) {
                        NSString *name = [NSString stringWithUTF8String:key];
                        if (name) [keys addObject:name];
                    }
                }
            }
        }
        unsigned cycle = 0;
        unsigned retry_after[256] = {0};
        while (getppid() == parent) {
            @autoreleasepool {
                NSMutableArray *sensors = [NSMutableArray array];
                unsigned index = 0;
                for (NSString *name in keys) {
                    unsigned sensor_index = index++;
                    if (cycle < retry_after[sensor_index]) continue;
                    const char *key = name.UTF8String;
                    double value = read_sensor(connection, keycode(key));
                    NSString *kind = key[0] == 'T' ? @"temperature" : key[0] == 'P' ? @"power" : key[0] == 'V' ? @"voltage" : @"fan";
                    NSString *unit = key[0] == 'T' ? @"°C" : key[0] == 'P' ? @"W" : key[0] == 'V' ? @"V" : @"RPM";
                    double maximum = key[0] == 'T' ? 150 : key[0] == 'V' ? 100 : 30000;
                    if (isfinite(value) && value > 0 && value < maximum)
                        [sensors addObject:@{@"id": [@"smc:" stringByAppendingString:name], @"kind": kind, @"unit": unit, @"value": @(value)}];
                    else retry_after[sensor_index] = cycle + 30;
                }
                NSDictionary *sample = @{@"sensors": sensors, @"thermal_critical": @(NSProcessInfo.processInfo.thermalState == NSProcessInfoThermalStateCritical),
                                          @"permission_denied": @(open_status == kIOReturnNotPrivileged),
                                          @"smc_available": @(connection != 0)};
                NSData *data = [NSJSONSerialization dataWithJSONObject:sample options:0 error:nil];
                fwrite(data.bytes, 1, data.length, stdout); puts(""); fflush(stdout);
            }
            ++cycle;
            sleep(1);
        }
        if (connection) IOServiceClose(connection);
        return 0;
    }
}

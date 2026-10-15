/* Read-only Metal inventory. No command queues, allocations or GPU workload. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <stdio.h>

int main(void) {
    @autoreleasepool {
        NSMutableArray *rows = [NSMutableArray array];
        for (id<MTLDevice> gpu in MTLCopyAllDevices()) {
            NSMutableDictionary *row = [@{@"name": gpu.name,
                @"registry_id": [NSString stringWithFormat:@"%llu", (unsigned long long)gpu.registryID],
                @"unified": @(gpu.hasUnifiedMemory),
                @"recommended_working_set": @(gpu.recommendedMaxWorkingSetSize),
                @"max_buffer_length": @(gpu.maxBufferLength),
                @"low_power": @(gpu.lowPower), @"removable": @(gpu.removable)} mutableCopy];
            NSMutableArray *families = [NSMutableArray array];
            if ([gpu supportsFamily:MTLGPUFamilyMetal3]) [families addObject:@"Metal 3"];
            if ([gpu supportsFamily:MTLGPUFamilyMac2]) [families addObject:@"Mac 2"];
            row[@"families"] = families;
            [rows addObject:row];
        }
        NSData *data = [NSJSONSerialization dataWithJSONObject:rows options:0 error:nil];
        if (!data) return 1;
        fwrite(data.bytes, 1, data.length, stdout);
        putchar('\n');
    }
    return 0;
}

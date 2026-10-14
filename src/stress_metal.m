/* Metal compute stress, ordinary permissions, bounded dispatches, parent watchdog. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <unistd.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

static uint32_t reference(uint32_t index, uint32_t seed) {
    uint32_t x = index ^ seed;
    for (unsigned j = 0; j < 1024; ++j) x = (x ^ (x >> 16)) * 1664525u + 1013904223u;
    return x;
}

int main(int argc, char **argv) {
    @autoreleasepool {
        id<MTLDevice> device = MTLCreateSystemDefaultDevice();
        if (argc == 2 && !strcmp(argv[1], "--probe")) {
            NSDictionary *info = device ? @{ @"available": @YES, @"backend": @"Metal", @"name": device.name } : @{ @"available": @NO };
            NSData *data = [NSJSONSerialization dataWithJSONObject:info options:0 error:nil];
            fwrite(data.bytes, 1, data.length, stdout); puts("");
            return 0;
        }
        if (!device || argc != 2) return 77;
        pid_t parent = (pid_t)strtol(argv[1], NULL, 10);
        if (parent <= 1 || parent != getppid()) return 2;
        NSString *source = @"#include <metal_stdlib>\nusing namespace metal;\nkernel void burn(device uint *out [[buffer(0)]], constant uint &seed [[buffer(1)]], uint i [[thread_position_in_grid]]) { uint x=i^seed; for(uint j=0;j<1024;++j)x=(x^(x>>16))*1664525u+1013904223u;out[i]=x;}";
        NSError *error = nil;
        id<MTLLibrary> library = [device newLibraryWithSource:source options:nil error:&error];
        id<MTLFunction> function = [library newFunctionWithName:@"burn"];
        id<MTLComputePipelineState> pipeline = function ? [device newComputePipelineStateWithFunction:function error:&error] : nil;
        id<MTLCommandQueue> queue = [device newCommandQueue];
        id<MTLBuffer> buffer = [device newBufferWithLength:65536 * sizeof(uint32_t) options:MTLResourceStorageModeShared];
        if (!pipeline || !queue || !buffer) { puts("error backend_error"); return 1; }
        uint32_t seed = 1;
        double heartbeat = 0;
        while (getppid() == parent) {
            @autoreleasepool {
                id<MTLCommandBuffer> commands = [queue commandBuffer];
                id<MTLComputeCommandEncoder> encoder = [commands computeCommandEncoder];
                [encoder setComputePipelineState:pipeline];
                [encoder setBuffer:buffer offset:0 atIndex:0];
                [encoder setBytes:&seed length:sizeof(seed) atIndex:1];
                NSUInteger group = MIN((NSUInteger)256, pipeline.maxTotalThreadsPerThreadgroup);
                [encoder dispatchThreads:MTLSizeMake(65536, 1, 1) threadsPerThreadgroup:MTLSizeMake(group, 1, 1)];
                [encoder endEncoding]; [commands commit]; [commands waitUntilCompleted];
                if (commands.status == MTLCommandBufferStatusError) { puts("error backend_error"); return 1; }
                uint32_t *values = buffer.contents;
                for (unsigned i = 0; i < 16; ++i)
                    if (values[i] != reference(i, seed)) { puts("error calculation_error"); return 1; }
                ++seed;
                double now = NSProcessInfo.processInfo.systemUptime;
                if (now - heartbeat >= 1) { puts("ready"); fflush(stdout); heartbeat = now; }
            }
        }
        return 0;
    }
}

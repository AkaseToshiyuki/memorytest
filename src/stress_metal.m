/* Metal compute stress, ordinary permissions, bounded dispatches, parent watchdog. */
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <unistd.h>
#include <stdint.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>

enum { InFlight = 3, GridSize = 262144 };

static id<MTLCommandBuffer> submit(id<MTLCommandQueue> queue,
                                  id<MTLComputePipelineState> pipeline,
                                  id<MTLBuffer> buffer, uint32_t seed) {
    id<MTLCommandBuffer> commands = [queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [commands computeCommandEncoder];
    if (!commands || !encoder) return nil;
    [encoder setComputePipelineState:pipeline];
    [encoder setBuffer:buffer offset:0 atIndex:0];
    [encoder setBytes:&seed length:sizeof(seed) atIndex:1];
    NSUInteger width = pipeline.threadExecutionWidth;
    NSUInteger group = MIN((NSUInteger)256, pipeline.maxTotalThreadsPerThreadgroup);
    group = MAX(width, group / width * width);
    [encoder dispatchThreads:MTLSizeMake(GridSize, 1, 1) threadsPerThreadgroup:MTLSizeMake(group, 1, 1)];
    [encoder endEncoding];
    [commands commit];
    return commands;
}

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
        if (!pipeline || !queue) { puts("error backend_error"); return 1; }
        id<MTLBuffer> buffers[InFlight];
        id<MTLCommandBuffer> pending[InFlight];
        uint32_t seeds[InFlight];
        uint32_t seed = 1;
        for (unsigned i = 0; i < InFlight; ++i) {
            buffers[i] = [device newBufferWithLength:GridSize * sizeof(uint32_t) options:MTLResourceStorageModeShared];
            if (!buffers[i]) { puts("error backend_error"); return 1; }
            seeds[i] = seed++;
            pending[i] = submit(queue, pipeline, buffers[i], seeds[i]);
            if (!pending[i]) { puts("error backend_error"); return 1; }
        }
        unsigned slot = 0;
        double heartbeat = 0;
        while (getppid() == parent) {
            @autoreleasepool {
                /* Keep the next two dispatches queued while checking this one.
                 * Never read or overwrite a buffer still owned by the GPU. */
                [pending[slot] waitUntilCompleted];
                if (pending[slot].status != MTLCommandBufferStatusCompleted) { puts("error backend_error"); return 1; }
                uint32_t *values = buffers[slot].contents;
                for (unsigned i = 0; i < 16; ++i) {
                    unsigned index = i * (GridSize / 16);
                    if (values[index] != reference(index, seeds[slot])) { puts("error calculation_error"); return 1; }
                }
                if (getppid() != parent) break;
                seeds[slot] = seed++;
                pending[slot] = submit(queue, pipeline, buffers[slot], seeds[slot]);
                if (!pending[slot]) { puts("error backend_error"); return 1; }
                slot = (slot + 1) % InFlight;
                double now = NSProcessInfo.processInfo.systemUptime;
                if (now - heartbeat >= 1) { puts("ready"); fflush(stdout); heartbeat = now; }
            }
        }
        for (unsigned i = 0; i < InFlight; ++i) [pending[i] waitUntilCompleted];
        return 0;
    }
}

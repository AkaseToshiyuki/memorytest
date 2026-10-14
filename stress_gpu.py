"""Driver-provided OpenCL 1.2 GPU compute. Never selects a CPU OpenCL device."""
import ctypes as C
import ctypes.util
from collections import deque
import json
import os
import sys
import time

from process_control import Cancellation

KERNEL = b"""
__kernel void burn(__global uint *out, uint seed) {
    uint i = get_global_id(0), x = i ^ seed;
    for (uint j = 0; j < 1024; ++j) x = (x ^ (x >> 16)) * 1664525u + 1013904223u;
    out[i] = x;
}
"""
GRID_SIZE = 262144
IN_FLIGHT = 3
SEED_COUNT = 64


def reference(index, seed):
    value = index ^ seed
    for _ in range(1024):
        value = ((value ^ (value >> 16)) * 1664525 + 1013904223) & 0xffffffff
    return value


class OpenCL:
    def __init__(self):
        path = "OpenCL.dll" if os.name == "nt" else ctypes.util.find_library("OpenCL")
        if not path:
            raise RuntimeError("device_unavailable")
        self.lib = (C.WinDLL if os.name == "nt" else C.CDLL)(path)
        self.context = self.queue = self.program = self.kernel = None
        self.slots = deque()
        self.bind("clGetPlatformIDs", C.c_int, C.c_uint, C.POINTER(C.c_void_p), C.POINTER(C.c_uint))
        self.bind("clGetDeviceIDs", C.c_int, C.c_void_p, C.c_uint64, C.c_uint, C.POINTER(C.c_void_p), C.POINTER(C.c_uint))
        self.bind("clGetDeviceInfo", C.c_int, C.c_void_p, C.c_uint, C.c_size_t, C.c_void_p, C.POINTER(C.c_size_t))
        self.bind("clCreateContext", C.c_void_p, C.c_void_p, C.c_uint, C.POINTER(C.c_void_p), C.c_void_p, C.c_void_p, C.POINTER(C.c_int))
        self.bind("clCreateCommandQueue", C.c_void_p, C.c_void_p, C.c_void_p, C.c_uint64, C.POINTER(C.c_int))
        self.bind("clCreateProgramWithSource", C.c_void_p, C.c_void_p, C.c_uint, C.POINTER(C.c_char_p), C.POINTER(C.c_size_t), C.POINTER(C.c_int))
        self.bind("clBuildProgram", C.c_int, C.c_void_p, C.c_uint, C.POINTER(C.c_void_p), C.c_char_p, C.c_void_p, C.c_void_p)
        self.bind("clCreateKernel", C.c_void_p, C.c_void_p, C.c_char_p, C.POINTER(C.c_int))
        self.bind("clCreateBuffer", C.c_void_p, C.c_void_p, C.c_uint64, C.c_size_t, C.c_void_p, C.POINTER(C.c_int))
        self.bind("clSetKernelArg", C.c_int, C.c_void_p, C.c_uint, C.c_size_t, C.c_void_p)
        self.bind("clEnqueueNDRangeKernel", C.c_int, C.c_void_p, C.c_void_p, C.c_uint, C.c_void_p, C.POINTER(C.c_size_t), C.c_void_p, C.c_uint, C.c_void_p, C.c_void_p)
        self.bind("clEnqueueReadBuffer", C.c_int, C.c_void_p, C.c_void_p, C.c_uint, C.c_size_t, C.c_size_t, C.c_void_p, C.c_uint, C.c_void_p, C.c_void_p)
        self.bind("clWaitForEvents", C.c_int, C.c_uint, C.POINTER(C.c_void_p))
        self.bind("clFlush", C.c_int, C.c_void_p)
        self.bind("clFinish", C.c_int, C.c_void_p)
        for suffix in ("Event", "MemObject", "Kernel", "Program", "CommandQueue", "Context"):
            self.bind("clRelease" + suffix, C.c_int, C.c_void_p)
        count = C.c_uint()
        self.check(self.lib.clGetPlatformIDs(0, None, C.byref(count)))
        if not 0 < count.value <= 64:
            raise RuntimeError("device_unavailable")
        platforms = (C.c_void_p * count.value)()
        self.check(self.lib.clGetPlatformIDs(count.value, platforms, None))
        self.device = None
        for platform in platforms:
            device = C.c_void_p()
            if self.lib.clGetDeviceIDs(platform, 4, 1, C.byref(device), None) == 0:
                self.device = device
                break
        if self.device is None:
            raise RuntimeError("device_unavailable")
        name = C.create_string_buffer(512)
        self.check(self.lib.clGetDeviceInfo(self.device, 0x102B, len(name), name, None))
        self.name = name.value.decode("utf-8", errors="replace")

    def bind(self, name, result, *args):
        function = getattr(self.lib, name)
        function.restype, function.argtypes = result, args

    @staticmethod
    def check(code):
        if code:
            raise RuntimeError("backend_error")

    def initialize(self):
        error = C.c_int()
        self.context = self.lib.clCreateContext(None, 1, C.byref(self.device), None, None, C.byref(error))
        self.check(error.value)
        self.queue = self.lib.clCreateCommandQueue(self.context, self.device, 0, C.byref(error))
        self.check(error.value)
        source = C.c_char_p(KERNEL)
        self.program = self.lib.clCreateProgramWithSource(self.context, 1, C.byref(source), None, C.byref(error))
        self.check(error.value)
        self.check(self.lib.clBuildProgram(self.program, 1, C.byref(self.device), b"-cl-std=CL1.2", None, None))
        self.kernel = self.lib.clCreateKernel(self.program, b"burn", C.byref(error))
        self.check(error.value)
        # Fixed rotating seeds permit independent checks without a Python
        # reference loop in the hot path. Three 1-MiB GPU buffers are bounded.
        self.expected = {seed: tuple(reference(i, seed) for i in range(16))
                         for seed in range(1, SEED_COUNT + 1)}
        self.next_seed = 1
        for _ in range(IN_FLIGHT):
            buffer = self.lib.clCreateBuffer(self.context, 2, GRID_SIZE * 4, None, C.byref(error))
            slot = {"buffer": buffer, "output": (C.c_uint * 16)(), "event": C.c_void_p()}
            self.slots.append(slot)
            self.check(error.value)
            self.submit(slot)

    def submit(self, slot):
        pointer = C.c_void_p(slot["buffer"])
        self.check(self.lib.clSetKernelArg(self.kernel, 0, C.sizeof(pointer), C.byref(pointer)))
        slot["seed"] = self.next_seed
        self.next_seed = self.next_seed % SEED_COUNT + 1
        seed_arg, size = C.c_uint(slot["seed"]), C.c_size_t(GRID_SIZE)
        self.check(self.lib.clSetKernelArg(self.kernel, 1, C.sizeof(seed_arg), C.byref(seed_arg)))
        self.check(self.lib.clEnqueueNDRangeKernel(self.queue, self.kernel, 1, None, C.byref(size), None, 0, None, None))
        # Enqueue the nonblocking read immediately after its kernel; waiting on
        # this event will not wait for the later two kernels in the queue.
        self.check(self.lib.clEnqueueReadBuffer(self.queue, slot["buffer"], 0, 0,
                   C.sizeof(slot["output"]), slot["output"], 0, None, C.byref(slot["event"])))
        self.check(self.lib.clFlush(self.queue))

    def step(self):
        slot = self.slots[0]
        self.check(self.lib.clWaitForEvents(1, C.byref(slot["event"])))
        if tuple(slot["output"]) != self.expected[slot["seed"]]:
            raise RuntimeError("calculation_error")
        self.check(self.lib.clReleaseEvent(slot["event"]))
        slot["event"] = C.c_void_p()
        self.submit(slot)
        self.slots.rotate(-1)

    def close(self):
        if self.queue:
            self.lib.clFinish(self.queue)  # host read destinations must stay alive
        for slot in self.slots:
            if slot["event"]:
                self.lib.clReleaseEvent(slot["event"])
            if slot["buffer"]:
                self.lib.clReleaseMemObject(slot["buffer"])
        self.slots.clear()
        for attr, suffix in (("kernel", "Kernel"), ("program", "Program"), ("queue", "CommandQueue"), ("context", "Context")):
            if getattr(self, attr, None):
                getattr(self.lib, "clRelease" + suffix)(getattr(self, attr))
                setattr(self, attr, None)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args(argv)
    backend = None
    try:
        backend = OpenCL()
        if args.probe:
            print(json.dumps({"available": True, "name": backend.name, "backend": "OpenCL"}))
            return 0
        if not args.parent_pid:
            parser.error("a supervising parent is required")
        backend.initialize()
        cancelled = Cancellation(parent_pid=args.parent_pid)
        heartbeat = 0
        while not cancelled():
            backend.step()
            now = time.monotonic()
            if now - heartbeat >= 1:
                print("ready", flush=True)
                heartbeat = now
        return 0
    except (OSError, RuntimeError, AttributeError) as exc:
        if args.probe:
            print(json.dumps({"available": False, "reason": "device_unavailable"}))
            return 0
        print("error " + (str(exc) if str(exc) in {"calculation_error", "device_unavailable"} else "backend_error"), flush=True)
        return 1
    finally:
        if backend:
            backend.close()


if __name__ == "__main__":
    sys.exit(main())

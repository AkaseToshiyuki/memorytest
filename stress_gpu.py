"""Driver-provided OpenCL 1.2 GPU compute. Never selects a CPU OpenCL device."""
import ctypes as C
import ctypes.util
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
        self.context = self.queue = self.program = self.kernel = self.buffer = None
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
        for suffix in ("MemObject", "Kernel", "Program", "CommandQueue", "Context"):
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
        self.buffer = self.lib.clCreateBuffer(self.context, 2, 65536 * 4, None, C.byref(error))
        self.check(error.value)
        pointer = C.c_void_p(self.buffer)
        self.check(self.lib.clSetKernelArg(self.kernel, 0, C.sizeof(pointer), C.byref(pointer)))

    def step(self, seed):
        seed_arg, size = C.c_uint(seed), C.c_size_t(65536)
        self.check(self.lib.clSetKernelArg(self.kernel, 1, C.sizeof(seed_arg), C.byref(seed_arg)))
        self.check(self.lib.clEnqueueNDRangeKernel(self.queue, self.kernel, 1, None, C.byref(size), None, 0, None, None))
        output = (C.c_uint * 16)()
        self.check(self.lib.clEnqueueReadBuffer(self.queue, self.buffer, 1, 0, C.sizeof(output), output, 0, None, None))
        if any(value != reference(index, seed) for index, value in enumerate(output)):
            raise RuntimeError("calculation_error")

    def close(self):
        for attr, suffix in (("buffer", "MemObject"), ("kernel", "Kernel"), ("program", "Program"), ("queue", "CommandQueue"), ("context", "Context")):
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
        seed, heartbeat = 1, 0
        while not cancelled():
            backend.step(seed)
            seed = (seed + 1) & 0xffffffff
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

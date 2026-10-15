"""Sensor identities, not guessed core temperatures. SMC mappings: Stats (MIT).

Reference: github.com/exelban/stats/blob/c9e7ebe3390a84e3075946ffe39dda709c4747b1/Modules/Sensors/values.swift.
Adapted key/name/platform data: Copyright (c) 2019 Serhiy Mytrovtsiy, MIT;
the complete attribution and license are in packaging/Stats-MIT.txt.
Apple Silicon SMC entries identify thermal regions, not individual CPU cores.
Unknown model/key combinations deliberately retain their raw hardware identifier.
"""
import re
from i18n import ref

SMC_ZONES = {
    "M1": {"p_zone": "Tp01 Tp05 Tp0D Tp0H Tp0L Tp0P Tp0X Tp0b", "e_zone": "Tp09 Tp0T",
           "gpu_zone": "Tg05 Tg0D Tg0L Tg0T", "memory_zone": "Tm02 Tm06 Tm08 Tm09"},
    "M2": {"p_zone": "Tp01 Tp05 Tp09 Tp0D Tp0X Tp0b Tp0f Tp0j", "e_zone": "Tp1h Tp1t Tp1p Tp1l",
           "gpu_zone": "Tg0f Tg0j"},
    "M3": {"p_zone": "Tf04 Tf09 Tf0A Tf0B Tf0D Tf0E Tf44 Tf49 Tf4A Tf4B Tf4D Tf4E", "e_zone": "Te05 Te0L Te0P Te0S",
           "gpu_zone": "Tf14 Tf18 Tf19 Tf1A Tf24 Tf28 Tf29 Tf2A"},
    "M4": {"p_zone": "Tp01 Tp05 Tp09 Tp0D Tp0V Tp0Y Tp0b Tp0e", "e_zone": "Te05 Te0S Te09 Te0H",
           "gpu_zone": "Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k", "memory_zone": "Tm0p Tm1p Tm2p"},
}
SMC_PROFILES = {name: generation for generation, variants in {
    "M1": ("M1", "M1 Pro", "M1 Max", "M1 Ultra"),
    "M2": ("M2", "M2 Pro", "M2 Max", "M2 Ultra"),
    "M3": ("M3", "M3 Pro", "M3 Max", "M3 Ultra"),
    "M4": ("M4", "M4 Pro", "M4 Max"),
}.items() for name in variants}
SMC_GPU_VARIANTS = {
    "M4": "Tg0G Tg0H Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k",
    "M4 Pro": "Tg1U Tg1k Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k",
    "M4 Max": "Tg1U Tg1k Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k",
}
SMC_COMMON = {"TC0D": "cpu_die", "TC0H": "cpu_heatsink", "TC0P": "cpu_proximity",
    "TCAD": "cpu_package", "TG0D": "gpu_die", "TG0H": "gpu_heatsink", "TG0P": "gpu_proximity",
    "Tm0P": "mainboard", "TW0P": "wireless", "TL0P": "display_device", "TB1T": "battery"}
SMC_APPLE = {"TaLP": "airflow_left", "TaRF": "airflow_right", "TH0x": "nand",
             "TB1T": "battery_1", "TB2T": "battery_2"}
SMC_SIGNALS = {
    "power": {"PC0C": "cpu_core_power", "PCPC": "cpu_package", "PCTR": "cpu_total",
              "PCPT": "cpu_package_total", "PCPR": "cpu_package_total", "PG0C": "gpu",
              "PG0R": "gpu_1", "PG1R": "gpu_2", "PMTR": "memory_total_sensor", "PSTR": "system_total"},
    "voltage": {"VCAC": "cpu_ia", "VCSC": "cpu_system_agent", "VCTC": "intel_graphics",
                "VG0C": "gpu", "VM0R": "memory_total_sensor", "VD0R": "dc_input"},
}


def smc_profile(model):
    name = re.sub(r"^Apple\s+", "", model.strip())
    return name if name in SMC_PROFILES else None


def smc_label(key, kind, model=""):
    profile = smc_profile(model)
    if kind == "temperature" and profile:
        zones = dict(SMC_ZONES[SMC_PROFILES[profile]])
        if profile in SMC_GPU_VARIANTS:
            zones["gpu_zone"] = SMC_GPU_VARIANTS[profile]
        for zone, keys in zones.items():
            sensors = keys.split()
            if key in sensors:
                return ref("identified_sensor", name=ref("numbered_zone", name=ref(zone), number=sensors.index(key) + 1), identity=key)
        if key in SMC_APPLE:
            return ref("identified_sensor", name=ref(SMC_APPLE[key]), identity=key)
    if kind == "temperature" and key in SMC_COMMON:
        return ref("identified_sensor", name=ref(SMC_COMMON[key]), identity=key)
    if key in SMC_SIGNALS.get(kind, {}):
        return ref("identified_sensor", name=ref(SMC_SIGNALS[kind][key]), identity=key)
    if kind == "fan" and re.fullmatch(r"F[0-9]Ac", key):
        return ref("identified_sensor", name=ref("numbered_fan", number=int(key[1]) + 1), identity=key)
    return ref("unmapped_sensor", kind=ref("thermal_zone" if kind == "temperature" else kind), identity="SMC " + key)


def named_sensor(name, identity, kind):
    """Use provider labels; translate common terms without destroying raw identity."""
    name = str(name or "").strip()[:120]
    keys = {"CPU Package": "cpu_package", "Package": "cpu_package", "GPU Core": "gpu_die",
            "Composite": "composite_sensor", "Tctl": "cpu_control", "Tdie": "cpu_die", "CPU Total": "cpu_usage",
            "Core Average": "core_average", "Core Max": "core_max", "GPU Hot Spot": "gpu_hotspot", "GPU Memory Junction": "gpu_memory_junction"}
    label = ref(keys[name]) if name in keys else name
    match = re.fullmatch(r"(?:CPU )?Core\s*#?(\d+)", name, re.I)
    if match:
        label = ref("numbered_core", number=int(match[1]))
    match = re.fullmatch(r"(?:CPU )?Core\s*#?(\d+) Distance to TjMax", name, re.I)
    if match:
        label = ref("core_temperature_margin", number=int(match[1]))
    match = re.fullmatch(r"(?:CPU )?Core\s*#?(\d+) \(Effective\)", name, re.I)
    if match:
        label = ref("core_effective_frequency", number=int(match[1]))
    match = re.fullmatch(r"Package id (\d+)", name)
    if match:
        label = ref("numbered_package", number=int(match[1]))
    return ref("identified_sensor", name=label, identity=identity) if name else ref("unmapped_sensor", kind=ref(kind), identity=identity)

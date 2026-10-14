"""Sensor identities, not guessed core temperatures. SMC mappings: Stats (MIT).

Reference: github.com/exelban/stats/Modules/Sensors/values.swift.
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
           "gpu_zone": "Tg0G Tg0H Tg1U Tg1k Tg0K Tg0L Tg0d Tg0e Tg0j Tg0k", "memory_zone": "Tm0p Tm1p Tm2p"},
}
SMC_COMMON = {"TC0D": "cpu_die", "TC0H": "cpu_heatsink", "TC0P": "cpu_proximity",
    "TCAD": "cpu_package", "TG0D": "gpu_die", "TG0H": "gpu_heatsink", "TG0P": "gpu_proximity",
    "Tm0P": "mainboard", "TW0P": "wireless", "TL0P": "display_device", "TB1T": "battery"}


def smc_label(key, kind, model=""):
    match = re.search(r"\b(M[1-9][0-9]*)\b", model)
    if match:
        for zone, keys in SMC_ZONES.get(match[1], {}).items():
            if key in keys.split():
                return ref("identified_sensor", name=ref(zone), identity=key)
    if key in SMC_COMMON:
        return ref("identified_sensor", name=ref(SMC_COMMON[key]), identity=key)
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

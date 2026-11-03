"""Optional device-wide NVIDIA telemetry, never attributed to one HORUS process."""

import csv
import io
import shutil
import subprocess
import threading
import time


def parse_devices(text):
    names = ("gpu_percent", "memory_controller_percent", "memory_used_mib", "memory_total_mib",
             "encoder_percent", "decoder_percent")
    devices = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) != len(names) + 1:
            continue
        device = {"uuid": row[0].strip(), "scope": "device_total_not_connector_only"}
        for name, value in zip(names, row[1:]):
            try:
                device[name] = float(value.strip())
            except ValueError:
                device[name] = None
        devices.append(device)
    return devices


class GpuSampler:
    def __init__(self):
        self.lock = threading.Lock()
        self.updated = -float("inf")
        self.devices = []

    def sample(self):
        with self.lock:
            now = time.monotonic()
            if now - self.updated >= 5:
                self.updated = now
                self.devices = []
                executable = shutil.which("nvidia-smi")
                if executable:
                    try:
                        result = subprocess.run([executable,
                            "--query-gpu=uuid,utilization.gpu,utilization.memory,memory.used,memory.total,utilization.encoder,utilization.decoder",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=1, check=True)
                        self.devices = parse_devices(result.stdout)
                    except (OSError, subprocess.SubprocessError):
                        pass
            return [dict(device) for device in self.devices]


sampler = GpuSampler()

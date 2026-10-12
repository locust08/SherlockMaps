"""Host available-memory estimates; never substitute invented capacity."""
from __future__ import annotations

import ctypes
import platform
import re
import subprocess
from pathlib import Path


def available_memory() -> dict:
    """Available, not strictly unused RAM, in GiB. Unknown is explicit.

    macOS estimate includes free, inactive and speculative pages, not wired,
    active or compressed pages. It is a capacity estimate, not memory pressure.
    """
    source = 'unavailable'
    try:
        system = platform.system()
        if system == 'Darwin':
            output = subprocess.run(['/usr/bin/vm_stat'], capture_output=True, text=True,
                                    check=True, timeout=2).stdout
            size = re.search(r'page size of (\d+) bytes', output)
            if not size:
                raise ValueError('Missing page size')
            pages = []
            for label in ('free', 'inactive', 'speculative'):
                match = re.search(rf'^Pages {label}:\s+(\d+)\.', output, re.MULTILINE)
                if not match:
                    raise ValueError('Missing page count')
                pages.append(int(match.group(1)))
            byte_count = sum(pages) * int(size.group(1))
            source = 'macOS vm_stat estimate'
        elif system == 'Linux':
            output = Path('/proc/meminfo').read_text()
            match = re.search(r'^MemAvailable:\s+(\d+) kB', output, re.MULTILINE)
            if not match:
                raise ValueError('Missing MemAvailable')
            byte_count = int(match.group(1)) * 1024
            source = 'Linux MemAvailable'
        elif system == 'Windows':
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('memory_load', ctypes.c_ulong),
                            ('total_phys', ctypes.c_ulonglong), ('avail_phys', ctypes.c_ulonglong),
                            ('total_page', ctypes.c_ulonglong), ('avail_page', ctypes.c_ulonglong),
                            ('total_virtual', ctypes.c_ulonglong), ('avail_virtual', ctypes.c_ulonglong),
                            ('avail_extended_virtual', ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                raise OSError('Memory measurement failed')
            byte_count = status.avail_phys
            source = 'Windows available physical memory'
        else:
            raise ValueError('Unsupported OS')
        value = round(byte_count / 1024 ** 3, 2)
        return {'available_ram_gb': value, 'memory_source': source,
                'ram_operating_state': 'critical' if value < .5 else 'constrained' if value < 1.5 else 'healthy'}
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return {'available_ram_gb': None, 'memory_source': 'unavailable', 'ram_operating_state': 'unknown'}

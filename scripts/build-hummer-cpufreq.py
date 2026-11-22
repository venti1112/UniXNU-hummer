#!/usr/bin/env python3
"""Compile the hummer CPU frequency governor against the source-kernel ABI.

Ported from the REDMI K90 (annibale, same SM8750) port's annibale_cpufreq.c.
The clock of the processor cores is set by the CPUCP firmware; the operating
system asks it for a performance level through SCMI's performance protocol
(0x13), one message at a time in a shared-memory channel with a mailbox
doorbell after each. Domain 0 is cluster 0's six cores, domain 1 the two of
cluster 1.

The object is archived into build/hummer-cpufreq/libHummerCpufreq.a, which
hummer_defconfig force-loads via XNU_EXTRA_LD_ARCHIVES. The board entry point
(scripts/stage7/Stage7Probe.cpp) calls hummer_cpufreq_init() from its
post-IOKit late probe, the same place the GPU bring-up hangs.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/hummer-cpufreq'
OUT.mkdir(parents=True, exist_ok=True)

C_RECORD = ROOT / 'build/xnu/ios-obj/DEVELOPMENT_ARM64_BCM2837/osfmk/DEVELOPMENT/startup.o.json'
SOURCE = ROOT / 'kits/hummer/HummerCpufreq.c'
ARCHIVE = OUT / 'libHummerCpufreq.a'


def run(command, cwd):
    result = subprocess.run([str(v) for v in command], cwd=str(cwd),
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode:
        raise SystemExit(result.stdout[-4000:])
    return result.stdout


def replay(record_path, source, destination):
    record = json.loads(record_path.read_text().strip().rstrip(','))
    command = []
    arguments = record['arguments']
    index = 0
    # Same replay loop as build-hummer-touch.py: swap the input file, redirect
    # -o, drop warning-flag pairs. Combined dependency flags pass through.
    while index < len(arguments):
        value = arguments[index]
        if value == record['file']:
            command.append(str(source))
        elif value == '-o':
            command.extend(['-o', str(destination)])
            index += 1
        elif value == '-Xclang' and arguments[index + 1].startswith('-W'):
            index += 1
        elif not value.startswith('-W'):
            command.append(value)
        index += 1
    return command, record['directory']


for required in (C_RECORD, SOURCE):
    if not required.exists():
        raise SystemExit(f'{required} is missing; the cpufreq build cannot proceed')

command, directory = replay(C_RECORD, SOURCE, OUT / 'HummerCpufreq.o')
if command and command[0] in ('clang', 'clang++'):
    command = ['xcrun', '--sdk', 'iphoneos', command[0]] + command[1:]
# The governor includes <kern/...> (osfmk), <sys/...> (bsd) and the board's own
# header directory. No -Werror: engine headers are compiled as-is.
# Only osfmk needs to be appended: the compile record already carries
# EXPORT_HDRS/bsd, and adding the real bsd/ as well makes <libkern/libkern.h>
# resolve twice with conflicting copyinstr prototypes.
command += ['-I', str(ROOT / '.cache/xnu-work/osfmk'),
            '-I', str(ROOT / 'kits/hummer'),
            '-Wno-unused-parameter', '-Wno-unknown-warning-option']
(OUT / 'compile-command.json').write_text(json.dumps(command, indent=2) + '\n')
run(command, directory)

ARCHIVE.unlink(missing_ok=True)
subprocess.run(['xcrun', 'ar', 'rcs', ARCHIVE, OUT / 'HummerCpufreq.o'], check=True)
print('HummerCpufreq: libHummerCpufreq.a (SCMI performance governor)')

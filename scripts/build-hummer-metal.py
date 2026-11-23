#!/usr/bin/env python3
"""Build hummer's A830 Metal userspace and stage it into the Data seed.

Runs on the macOS CI runner inside the assembled UniXNU tree (out/hummer/tree),
after the kernel and the original trust bundle exist:

  - Mesa 85fda3b (sparse checkout) with the engine's recorded patches
  - Turnip for ARM64/iOS 17 with the native Darwin kernel backend
  - NativeAdrenoMetal.bundle + adrenoprobed (4 KiB-page ad-hoc signatures)
  - a Data seed carrying /private/var/ngpu/{addons,bin,daemons}
  - the owner trust bundle for the plugin, the probe daemon and Turnip

The Metal plugin then answers backboardd's Metal discovery through the addon
mount (boot-arg sheng-gpu-addon=1); the display hands its frames to the GPU
presenter (sheng-fb-gpu=1). Nothing here writes to Git or to Apple inputs.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def run(command, cwd=None, env=None):
    print('+', ' '.join(str(x) for x in command), flush=True)
    return subprocess.run([str(x) for x in command], cwd=cwd, env=env, check=True)


def read(command, cwd=None):
    return subprocess.check_output([str(x) for x in command], cwd=cwd, text=True).strip()


def require_tool(name, hint=''):
    path = shutil.which(name)
    if not path:
        sys.exit('missing build tool: %s%s' % (name, (' (%s)' % hint) if hint else ''))
    return path


def prepare_mesa(tree):
    pins = json.loads((tree / 'scripts/data/adreno-compiler-inputs.json').read_text())
    mesa = tree / '.cache/mesa-work'
    pin = pins['mesa']['commit']
    fresh = not (mesa / '.git').is_dir()
    if fresh:
        mesa.mkdir(parents=True)
        run(['git', 'init', '-q'], cwd=mesa)
        run(['git', 'remote', 'add', 'origin', pins['mesa']['mirror']], cwd=mesa)
        run(['git', 'fetch', '--depth=1', '--filter=blob:none', 'origin', pin], cwd=mesa)
        run(['git', 'sparse-checkout', 'init', '--cone'], cwd=mesa)
        run(['git', 'sparse-checkout', 'set', *pins['mesa']['sparse_directories']], cwd=mesa)
        run(['git', 'checkout', '-q', '--detach', 'FETCH_HEAD'], cwd=mesa)
    head = read(['git', 'rev-parse', 'HEAD'], cwd=mesa)
    if head != pin:
        sys.exit('Mesa source differs from the recorded pin: %s' % head)
    if fresh or not read(['git', 'status', '--porcelain'], cwd=mesa):
        for patch in pins['patches']:
            run(['git', 'apply', str(tree / patch)], cwd=mesa)
    print('mesa ready at', pin)


def prepare_translator(tree):
    pins = json.loads((tree / 'scripts/data/gpu-metal-vulkan-inputs.json').read_text())['translator']
    source = tree / '.cache/gpu-reference/metal2vulkan'
    pin = pins['commit']
    if not (source / '.git').is_dir():
        source.mkdir(parents=True)
        run(['git', 'init', '-q'], cwd=source)
        run(['git', 'remote', 'add', 'origin', pins['repository']], cwd=source)
        run(['git', 'fetch', '--depth=1', '--filter=blob:none', 'origin', pin], cwd=source)
        run(['git', 'checkout', '-q', '--detach', 'FETCH_HEAD'], cwd=source)
    head = read(['git', 'rev-parse', 'HEAD'], cwd=source)
    if head != pin:
        sys.exit('metal2vulkan source differs from the recorded pin: %s' % head)
    print('metal2vulkan ready at', pin)


def prepare_venv(tree):
    venv = tree / '.cache/mesa-venv'
    if not (venv / 'bin/meson').is_file():
        run([sys.executable, '-m', 'venv', venv])
        run([venv / 'bin/pip', 'install', '-q', '-r', tree / 'scripts/requirements-adreno-compiler.txt'])
    tools = tree / '.cache/mesa-tools/bin'
    tools.mkdir(parents=True, exist_ok=True)
    bison = Path(require_tool('bison', 'brew install bison'))
    link = tools / 'bison'
    if not link.exists():
        link.symlink_to(bison)
    print('meson venv ready')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tree', type=Path, required=True, help='assembled UniXNU tree (out/hummer/tree)')
    parser.add_argument('--data', type=Path, required=True, help='original packed Data seed (data.dbrdisk)')
    parser.add_argument('--trust', type=Path, required=True, help='original static+System trust bundle from prepare-original-trust.py')
    parser.add_argument('--output', type=Path, required=True, help='directory for data-gpu.dbrdisk and owner-trust.bundle')
    parser.add_argument('--clients', default='WidgetRenderer_Default,PosterBoard,iconservicesagent',
                        help='processes besides backboardd that get a native Metal device')
    args = parser.parse_args()
    tree, data, trust, output = args.tree.resolve(), args.data.resolve(), args.trust.resolve(), args.output.resolve()

    if sys.platform != 'darwin':
        sys.exit('the iOS SDK, codesign and hdiutil require a macOS build host')
    for name, hint in (('xcrun', ''), ('codesign', ''), ('hdiutil', ''), ('git', ''),
                       ('glslangValidator', 'brew install glslang'), ('glslc', 'brew install shaderc'),
                       ('spirv-val', 'brew install spirv-tools'), ('cargo', 'rustup toolchain install 1.98.0'),
                       ('rustup', 'rustup toolchain install 1.98.0')):
        require_tool(name, hint)
    if not (tree / 'scripts/build-turnip-darwin.py').is_file():
        sys.exit('not a UniXNU tree: ' + str(tree))
    if not data.is_file() or not trust.is_file():
        sys.exit('missing Data seed or original trust bundle')

    prepare_mesa(tree)
    prepare_translator(tree)
    prepare_venv(tree)

    run([sys.executable, 'scripts/build-turnip-darwin.py', '--target', 'ios'], cwd=tree)
    run([sys.executable, 'scripts/build-adreno-metal-plugin.py'], cwd=tree)

    original = tree / 'build/original-static-system-trust'
    original.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(trust, original / 'original-static-system.bundle')
    run([sys.executable, 'scripts/make-owner-trust-bundle.py'], cwd=tree)

    output.mkdir(parents=True, exist_ok=True)
    staged = output / 'data-gpu.dbrdisk'
    staged.unlink(missing_ok=True)
    run([sys.executable, 'scripts/add-gpu-plugin-data.py', '--publish', '--clients', args.clients,
         str(data), str(staged)], cwd=tree)
    shutil.copyfile(tree / 'build/adreno-metal-plugin/owner-trust.bundle', output / 'owner-trust.bundle')
    shutil.copyfile(tree / 'build/adreno-metal-plugin/trust.json', output / 'plugin-trust.json')

    print('PASS: Turnip + NativeAdrenoMetal staged')
    print('  data:  ', staged)
    print('  trust: ', output / 'owner-trust.bundle')
    print('  boot args: sheng-gpu-addon=1 sheng-fb-gpu=1 hummer-adreno-bringup=1')


if __name__ == '__main__':
    main()

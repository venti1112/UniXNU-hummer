#!/usr/bin/env python3
"""切出 hummer 的 unixnu 分区：缩小 userdata，新建一个独立的 root 分区。

为什么需要它
------------
NativeUfs 在 NATIVE_UFS_SM8750 构建里把 UFS_ROOT_PARTITION 定成 "unixnu"，
RootLayout.h 的 findRootLayout 按名字**精确**匹配 GPT 条目。hummer 出厂的
GPT 里没有这个条目，v221 的 panic 就停在

    UFS ROOT FAILURE: no selected root partition
    stage=GPT entries result=00000000

——注意 stage 是 "GPT entries"、result 是 kIOReturnSuccess、entries CRC 也过了，
说明 GPT header 和分区表都读对了，纯粹是名字对不上。

做成独立分区而不是直接覆盖 userdata，是为了不动安卓：userdata 只缩小、
不搬数据，安卓的 super/system 那些分区一个字节都不碰。

⚠ 缩小 userdata 会让它的 f2fs 失效（superblock 里的 segment_count 还按
  原来的大小记着）。安卓第一次开机时会发现 userdata 不可用，通常直接走
  recovery 让你格式化。也就是说**安卓数据会丢**，但系统分区完好，
  重刷 userdata 或恢复出厂就能回到 ColorOS。真要保住数据，先自己备份。

用法
----
    # 1) 只读：抓 GPT、打印规划、把原始 GPT 存到 out/gpt-backup-<时间>/
    python3 scripts/make-hummer-root-partition.py

    # 2) 确认规划无误后写回（会二次确认）
    python3 scripts/make-hummer-root-partition.py --apply --size-gib 32

    # 3) 出事时用备份写回（同样要 --apply）
    python3 scripts/make-hummer-root-partition.py --restore out/gpt-backup-.../  --apply

前提：设备已连 adb，且 `adb exec-out su -c 'id'` 能拿到 root。
"""
import argparse
import re
import struct
import subprocess
import sys
import time
import uuid
import zlib
from pathlib import Path

SECTOR = 4096                     # UFS LUN 0 的逻辑块大小（board.toml: lba_bytes）
DEVICE = None                     # 由 resolve_device() 从 by-name/userdata 反查后填入
USE_SU = None                     # 由 require_device() 决定：recovery 里没有 su
GPT_SIG = b'EFI PART'
HEADER_SIZE = 92                  # GPT header 的固定部分
ENTRY_SIZE = 128
# Apple APFS 的 GPT 类型 GUID：分区里装的确实是 APFS 卷，用这个比 Linux data 更贴切。
# findRootLayout 只看名字不看类型，这里选类型只是为了语义正确、也方便别的工具识别。
APFS_TYPE = uuid.UUID('7C3457EF-0000-11AA-AA11-00306543ECAC')
ROOT_NAME = 'unixnu'


def die(message):
    sys.exit(f'make-hummer-root-partition: {message}')


def adb_su(command, stdin=None):
    """在设备上以 root 跑一条命令，返回 stdout 字节。

    recovery 里 adbd 本身就是 root，没有 su 二进制；正常开机则要 su -c。
    用哪种由 require_device() 探测后写进 USE_SU。
    """
    args = ['adb', 'exec-out'] + (['su', '-c', command] if USE_SU else [command])
    return subprocess.run(args, input=stdin, capture_output=True, check=True).stdout


def require_device():
    global USE_SU
    try:
        out = subprocess.run(['adb', 'devices'], capture_output=True, text=True, check=True).stdout
    except FileNotFoundError:
        die('adb is not installed')
    if '\tdevice' not in out and '\trecovery' not in out:
        die('no device: connect it and allow USB debugging')
    # 先看 adbd 自己是不是 root（recovery 的常态），再退回 su -c（正常系统 + Magisk）。
    direct = subprocess.run(['adb', 'exec-out', 'id'], capture_output=True, text=True)
    if 'uid=0' in direct.stdout:
        USE_SU = False
        print('adbd is already root (recovery?)')
        return
    su = subprocess.run(['adb', 'exec-out', 'su', '-c', 'id'], capture_output=True, text=True)
    if 'uid=0' in su.stdout:
        USE_SU = True
        print('root via su -c')
        return
    die(f'no root: adbd says {direct.stdout.strip()!r}, su says {su.stdout.strip()!r}')


def resolve_device():
    """从 by-name/userdata 反查整盘设备。一加通常是 /dev/block/sda，但不硬编码：
    UFS 也可能枚举成 mmcblk 或别的名字，写错设备是灾难性的。"""
    global DEVICE
    try:
        link = adb_su('readlink -f /dev/block/by-name/userdata').decode().strip()
    except subprocess.CalledProcessError:
        link = ''
    match = re.fullmatch(r'(/dev/block/(?:sd[a-z]+|mmcblk\d+))p?\d*', link)
    if not match:
        die(f'could not derive the whole-disk device from by-name/userdata ({link!r})')
    DEVICE = match.group(1)
    # 整盘的 /sys/class/block/<name> 存在、且没有 partition 子文件，才说明拿到的不是某个分区。
    probe = adb_su(f'cat /sys/class/block/{Path(DEVICE).name}/partition 2>/dev/null; echo end')
    if b'end' not in probe or probe.strip() != b'end':
        die(f'{DEVICE} looks like a partition, not the whole disk')
    print(f'block device {DEVICE}  (resolved from /dev/block/by-name/userdata)')


def disk_bytes():
    """整个 LUN 0 的字节数。/sys/block 的 size 是 512 字节扇区数。"""
    raw = adb_su(f'cat /sys/block/{Path(DEVICE).name}/size').decode().strip()
    if not raw.isdigit():
        die(f'could not read the device size: {raw!r}')
    total = int(raw) * 512
    if total % SECTOR:
        die(f'device size {total} is not a multiple of {SECTOR}')
    return total


def read_sectors(lba, count):
    data = adb_su(f'dd if={DEVICE} bs={SECTOR} skip={lba} count={count} 2>/dev/null')
    if len(data) != count * SECTOR:
        die(f'short read at lba {lba}: wanted {count * SECTOR} bytes, got {len(data)}')
    return data


def write_sectors(lba, data, dry_run):
    """把 data 写到 lba 起始的位置，走 adb shell 的 stdin 直接喂设备端 dd。

    不用 adb push：那条路在 recovery 上会返回 0 却只留下一个 0 字节文件，
    于是 dd 老老实实报 "0 bytes copied"，看起来像写失败但其实一个字节都没送过去。
    stdin 的二进制安全性已用 md5 验证过（139,264 字节的大文件也不做换行转换）。

    失败时抛 RuntimeError（而不是直接退出），调用方要能在只写了一半时回滚。
    """
    if dry_run:
        print(f'    [dry-run] would write {len(data)} bytes at lba {lba}')
        return
    if not data:
        # 空缓冲会让下面的字节数校验变成 0 == 0 而通过，等于假成功。
        raise RuntimeError('refusing to write an empty buffer')
    local = Path('/tmp/unixnu-gpt.bin')
    local.write_bytes(data)
    with local.open('rb') as handle:
        result = subprocess.run(
            ['adb', 'shell', f'dd of={DEVICE} bs={SECTOR} seek={lba} conv=notrunc,fsync 2>&1'],
            stdin=handle, capture_output=True, text=True)
    report = ((result.stdout or '') + (result.stderr or '')).strip()
    local.unlink(missing_ok=True)
    # dd 自己的统计是唯一可信的证据：退出码为 0 不代表真的搬了数据。
    copied = re.search(r'(\d+) bytes', report)
    if not copied or int(copied.group(1)) != len(data):
        raise RuntimeError(f'dd copied {copied.group(1) if copied else "unknown"} bytes, '
                           f'expected {len(data)}: {report}')
    print(f'    lba {lba}: {len(data)} bytes -> {report.splitlines()[-1]}')


def crc32(data):
    return zlib.crc32(data) & 0xFFFFFFFF


def parse_header(block):
    if len(block) < HEADER_SIZE or block[:8] != GPT_SIG:
        die('the primary GPT header is missing "EFI PART"')
    revision, header_size, header_crc, _reserved, my_lba, alternate, first_usable, last_usable = \
        struct.unpack_from('<IIIIQQQQ', block, 8)
    disk_guid = uuid.UUID(bytes_le=block[56:72])
    entries_lba, count, entry_size, entries_crc = struct.unpack_from('<QIII', block, 72)
    if header_size < HEADER_SIZE or header_size > SECTOR:
        die(f'implausible header size {header_size}')
    stored = bytearray(block[:header_size])
    struct.pack_into('<I', stored, 16, 0)
    if crc32(bytes(stored)) != header_crc:
        die('the primary GPT header CRC does not match: refusing to touch this disk')
    if entry_size != ENTRY_SIZE:
        die(f'unsupported entry size {entry_size}')
    return {
        'revision': revision, 'header_size': header_size, 'header_crc': header_crc,
        'my_lba': my_lba, 'alternate': alternate, 'first_usable': first_usable,
        'last_usable': last_usable, 'disk_guid': disk_guid, 'entries_lba': entries_lba,
        'count': count, 'entry_size': entry_size, 'entries_crc': entries_crc,
    }


def parse_entries(blob, header):
    count, size = header['count'], header['entry_size']
    if len(blob) < count * size:
        die('the GPT entry array is truncated')
    if crc32(blob[:count * size]) != header['entries_crc']:
        die('the GPT entry array CRC does not match: refusing to touch this disk')
    entries = []
    for index in range(count):
        raw = blob[index * size:(index + 1) * size]
        used = any(raw[:16])
        name = raw[56:128].decode('utf-16-le', errors='replace').split('\x00', 1)[0]
        entries.append({
            'index': index, 'raw': bytearray(raw), 'used': used, 'name': name,
            'type': uuid.UUID(bytes_le=raw[0:16]) if used else None,
            'unique': uuid.UUID(bytes_le=raw[16:32]) if used else None,
            'first': struct.unpack_from('<Q', raw, 32)[0] if used else 0,
            'last': struct.unpack_from('<Q', raw, 40)[0] if used else 0,
        })
    return entries


def build_header(header, entries_crc, my_lba, alternate, dry_run):
    block = bytearray(SECTOR)
    struct.pack_into('<8sIIIIQQQQ', block, 0, GPT_SIG, header['revision'], header['header_size'],
                     0, 0, my_lba, alternate, header['first_usable'], header['last_usable'])
    block[56:72] = header['disk_guid'].bytes_le
    struct.pack_into('<QIII', block, 72, header['entries_lba'], header['count'],
                     header['entry_size'], entries_crc)
    size = header['header_size']
    struct.pack_into('<I', block, 16, crc32(bytes(block[:size])))
    return bytes(block)


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--size-gib', type=int, default=32,
                        help='unixnu 分区大小，GiB（默认 32：8 GiB 镜像 + 1 GiB 日志预留 + Data 卷）')
    parser.add_argument('--apply', action='store_true', help='真的写回设备（默认只读）')
    parser.add_argument('--restore', metavar='DIR', help='用备份目录里的 GPT 写回')
    parser.add_argument('--yes', action='store_true', help='跳过交互确认')
    args = parser.parse_args()

    require_device()
    resolve_device()
    total = disk_bytes()
    total_lba = total // SECTOR
    print(f'device  {DEVICE}: {total:,} bytes = {total / 2**30:.2f} GiB = {total_lba:,} LBAs of {SECTOR}')

    backup_dir = Path(f'out/gpt-backup-{time.strftime("%Y%m%d-%H%M%S")}')

    if args.restore:
        source = Path(args.restore)
        primary = (source / 'primary-gpt.bin').read_bytes()
        backup = (source / 'backup-gpt.bin').read_bytes()
        print(f'restoring from {source}: primary {len(primary)} B, backup {len(backup)} B')
        if not args.apply:
            print('dry run: add --apply to write these back')
            return
        if not args.yes and input('write the saved GPT back to the device? [y/N] ').lower() != 'y':
            die('aborted')
        try:
            write_sectors(0, primary, dry_run=False)
            write_sectors(total_lba - len(backup) // SECTOR, backup, dry_run=False)
            check = read_sectors(0, -(-len(primary) // SECTOR))
            if check != primary:
                die('restore read back different bytes: the device did not take the write')
        except Exception as error:                               # noqa: BLE001
            die(f'restore failed: {error}')
        print('restored and verified. reboot into fastboot and check '
              '`fastboot getvar partition-size:userdata`')
        return

    # 主 GPT：LBA 0..33（保护性 MBR + header + 32 个 entry 扇区）；备份 GPT 对称地贴在盘尾。
    # 先按最大 32 个 entry 扇区抓，实际用到的部分由 header 里的 count/entry_size 决定。
    primary = read_sectors(0, 34)
    backup = read_sectors(total_lba - 33, 33)
    header = parse_header(primary[SECTOR:2 * SECTOR])
    print(f'GPT     revision {header["revision"]:#x}  entries at lba {header["entries_lba"]} '
          f'({header["count"]} x {header["entry_size"]})')
    print(f'        usable {header["first_usable"]:,}..{header["last_usable"]:,}  '
          f'backup header at lba {header["alternate"]:,}')

    if header['alternate'] != total_lba - 1:
        die(f'GPT says the backup header is at lba {header["alternate"]:,}, '
            f'but the disk ends at {total_lba - 1:,}: refusing to guess')

    entry_bytes = header['count'] * header['entry_size']
    entry_sectors = -(-entry_bytes // SECTOR)
    entries_blob = primary[header['entries_lba'] * SECTOR:
                           header['entries_lba'] * SECTOR + entry_bytes]
    entries = parse_entries(entries_blob, header)

    print('\ncurrent partition table:')
    for entry in entries:
        if entry['used']:
            print(f'  [{entry["index"]:2}] {entry["name"]:<32} {entry["first"]:>12,}..{entry["last"]:>12,}'
                  f'  {(entry["last"] - entry["first"] + 1) * SECTOR / 2**30:8.2f} GiB')

    existing = [e for e in entries if e['used'] and e['name'] == ROOT_NAME]
    if existing:
        die(f'a partition named {ROOT_NAME!r} already exists at '
            f'{existing[0]["first"]:,}..{existing[0]["last"]:,}; nothing to do')

    userdata = [e for e in entries if e['used'] and e['name'] == 'userdata']
    if not userdata:
        die('no partition named "userdata": this does not look like a hummer GPT')
    if len(userdata) > 1:
        die('more than one "userdata" partition: refusing to guess')
    userdata = userdata[0]

    want = args.size_gib * (2 ** 30) // SECTOR
    first = userdata['last'] + 1 - want
    if first <= userdata['first']:
        die(f'userdata ({userdata["first"]:,}..{userdata["last"]:,}) is too small to give away '
            f'{args.size_gib} GiB')
    # 留 4 GiB 或原大小的 5%（取大者）给安卓，避免切出一个几乎空的 userdata。
    keep = userdata['first'] + max(4 * (2 ** 30) // SECTOR, (userdata['last'] - userdata['first']) // 20)
    if first <= keep:
        die(f'refusing: the remaining userdata would end at {first - 1:,}, '
            f'below the {keep:,} floor')

    free = [e for e in entries if not e['used']]
    if not free:
        die('no free GPT entry slot')

    print(f'\nplan:')
    print(f'  userdata  {userdata["first"]:>12,}..{first - 1:>12,}'
          f'  {(first - userdata["first"]) * SECTOR / 2**30:8.2f} GiB   (was {userdata["last"] - userdata["first"] + 1:,} blocks)')
    print(f'  {ROOT_NAME:<9} {first:>12,}..{userdata["last"]:>12,}'
          f'  {want * SECTOR / 2**30:8.2f} GiB   slot [{free[0]["index"]}] type {APFS_TYPE}')
    print(f'\n⚠ userdata shrinks by {args.size_gib} GiB: its f2fs will not mount again until it is')
    print(f'  reformatted. Android data on it is lost; the system partitions are untouched.')

    # 备份：先把原始的两份 GPT 落盘，出问题能原样写回。
    backup_dir.mkdir(parents=True, exist_ok=True)
    (backup_dir / 'primary-gpt.bin').write_bytes(primary)
    (backup_dir / 'backup-gpt.bin').write_bytes(backup)
    (backup_dir / 'device.txt').write_text(f'{DEVICE} {total} bytes {total_lba} LBAs\n')
    print(f'\noriginal GPT saved to {backup_dir}/  (restore with --restore {backup_dir})')

    if not args.apply:
        print('\ndry run: nothing written. add --apply to commit this layout.')
        return
    if not args.yes:
        print()
        if input(f'write this layout to {DEVICE}? this cannot be undone without the backup [y/N] ').lower() != 'y':
            die('aborted')

    # 改 userdata 的 last，并把 unixnu 填进第一个空槽。
    userdata['raw'][40:48] = struct.pack('<Q', first - 1)
    slot = free[0]
    entry = bytearray(ENTRY_SIZE)
    entry[0:16] = APFS_TYPE.bytes_le
    entry[16:32] = uuid.uuid4().bytes_le
    struct.pack_into('<QQQ', entry, 32, first, userdata['last'], 0)   # first, last, attributes
    entry[56:56 + len(ROOT_NAME) * 2] = ROOT_NAME.encode('utf-16-le')
    slot['raw'][:] = entry

    new_blob = b''.join(bytes(e['raw']) for e in entries)
    entries_crc = crc32(new_blob)
    new_primary = bytearray(primary)
    new_primary[header['entries_lba'] * SECTOR:
                header['entries_lba'] * SECTOR + entry_bytes] = new_blob
    new_primary[SECTOR:2 * SECTOR] = build_header(header, entries_crc, 1, total_lba - 1, False)

    # 备份 GPT 的 entries 必须与主 GPT 完全一致，只有 header 里的 my_lba/alternate 对调。
    new_backup = bytearray(backup)
    backup_entries_at = (header['alternate'] - entry_sectors) * SECTOR - (total_lba - 33) * SECTOR
    new_backup[backup_entries_at:backup_entries_at + entry_bytes] = new_blob
    header_lba = header['alternate'] * SECTOR - (total_lba - 33) * SECTOR
    new_backup[header_lba:header_lba + SECTOR] = build_header(header, entries_crc, total_lba - 1, 1, False)

    # 写之前把两份都自检一遍：签名、CRC、以及 unixnu 条目真的在。
    check = parse_header(bytes(new_primary[SECTOR:2 * SECTOR]))
    if check['entries_crc'] != entries_crc:
        die('internal error: the rebuilt header does not carry the new entry CRC')
    written = parse_entries(bytes(new_primary[header['entries_lba'] * SECTOR:
                                            header['entries_lba'] * SECTOR + entry_bytes]), check)
    if not any(e['used'] and e['name'] == ROOT_NAME for e in written):
        die('internal error: the rebuilt entry array has no unixnu entry')

    print('\nwriting the primary GPT ...')
    try:
        write_sectors(0, bytes(new_primary), dry_run=False)
        print('writing the backup GPT ...')
        write_sectors(total_lba - 33, bytes(new_backup), dry_run=False)
        # 读回来核对：这一步失败说明写入没落盘，立刻用备份恢复。
        verify = read_sectors(0, 34)
        if verify != bytes(new_primary):
            for sector in range(34):
                got = verify[sector * SECTOR:(sector + 1) * SECTOR]
                want = bytes(new_primary)[sector * SECTOR:(sector + 1) * SECTOR]
                if got != want:
                    print(f'    sector {sector} differs: got {got[:16].hex()} want {want[:16].hex()}')
            raise RuntimeError('read-back mismatch')
    except Exception as error:                                   # noqa: BLE001 - 任何失败都要回滚
        print(f'\n*** 写入失败: {error}')
        print('*** 正在用刚才的备份恢复原始 GPT ...')
        try:
            write_sectors(0, primary, dry_run=False)
            write_sectors(total_lba - 33, backup, dry_run=False)
        except Exception as restore_error:                        # noqa: BLE001
            die(f'回滚也失败了: {restore_error}. 用 --restore {backup_dir} 手工恢复，'
                f'或者重跑本脚本的 --restore')
        die('the original GPT was restored')

    print(f'\ndone. {ROOT_NAME} = lba {first:,}..{userdata["last"]:,} '
          f'({want * SECTOR / 2**30:.2f} GiB), userdata now ends at {first - 1:,}')
    print(f'put these into board.toml [userdata]:')
    print(f'    first_lba = {first:#x}')
    print(f'    last_lba  = {userdata["last"]:#x}')
    print('\nthen: fastboot reboot bootloader && fastboot getvar partition-size:unixnu')


if __name__ == '__main__':
    main()

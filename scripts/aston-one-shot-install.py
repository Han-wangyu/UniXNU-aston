#!/usr/bin/env python3
"""End-to-end host precheck + TWRP adb one-shot deployer for OnePlus Ace 3 UniXNU.

Runs a strict fail-closed pipeline:
  0. Host validation (--check-only runs this stage cleanly with zero USB dependency):
     verifies boot_a (201326592 B + ANDROID! + AVBf), raw DMGs, vroots, wifi.obj,
     payload unixnu.toml (default='xnu' or --expected-default, args <= 1016 B),
     and SHNGDATA seed math.
  1. Target verification over TWRP adb: verifies serial, recovery/TWRP state, active
     slot '_b', block mappings for sda15 ('unixnu'), sda14 ('userdata'), sde13 ('boot_a'),
     and optional immutable SHA-256 verification of daily ColorOS boot_b (/dev/block/sde45),
     plus optional --format-userdata-if-needed F2FS superblock boundary check on sda14.
  2. Stages OS, SystemOS, OS.vroot, SystemOS.vroot, Payload, Wifi onto sda15 via
     flash-unixnu-staged.py --init-store-and-log (zeros SHNGDATA header/bitmap at
     block 4456335 + bootlog/export headers before committing SHNGROOT at offset 0).
  3. Writes boot_a via flash-aston-boot-a.py (only touches sde13; never boot_b).
  4. Re-verifies boot_b untouched, SHNGROOT header checksum, and SHNGDATA clean state.

Compatible with Python 3.9+.
"""
import argparse
import hashlib
import importlib.util
import json
import struct
import subprocess
import sys
from pathlib import Path

DEFAULT_SERIAL = 'ed5f732f'
BOOT_B_PART = '/dev/block/sde45'
PART_USERDATA = '/dev/block/sda14'
PART_UNIXNU = '/dev/block/sda15'
PART_BYTES = 34359738368
STORE_START_BLOCK = 4456335
F2FS_SUPER_MAGIC = 0xF2F52010
SCRIPTS_DIR = Path(__file__).resolve().parent


def require(ok, why):
    if not ok:
        raise SystemExit(f'STOP: {why}')


def _load_module(filename):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_').removesuffix('.py'), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def adb_run(adb_bin, serial, *cmd):
    res = subprocess.run([adb_bin, '-s', serial, *cmd], capture_output=True, check=True, stdin=subprocess.DEVNULL)
    return res.stdout.decode('utf-8', errors='replace').strip()


def adb_exec_bytes(adb_bin, serial, shell_cmd):
    res = subprocess.run(
        [adb_bin, '-s', serial, 'exec-out', shell_cmd],
        capture_output=True,
        check=True,
        stdin=subprocess.DEVNULL,
    )
    return res.stdout


def parse_f2fs_size_bytes(buf):
    if len(buf) < 1088:
        return None
    magic = struct.unpack_from('<I', buf, 1024)[0]
    if magic != F2FS_SUPER_MAGIC:
        return None
    log_blocksize = struct.unpack_from('<I', buf, 1024 + 12)[0]
    block_count = struct.unpack_from('<Q', buf, 1024 + 36)[0]
    if not (9 <= log_blocksize <= 16):
        return None
    return block_count << log_blocksize


def ensure_userdata_f2fs_fits(adb_bin, serial, allow_format):
    require(adb_run(adb_bin, serial, 'shell', 'readlink -f /dev/block/by-name/userdata') == PART_USERDATA,
            f'by-name/userdata does not resolve to {PART_USERDATA}')
    require(adb_run(adb_bin, serial, 'shell', 'readlink -f /dev/block/by-name/unixnu') == PART_UNIXNU,
            f'by-name/unixnu does not resolve to {PART_UNIXNU}')

    part_bytes = int(adb_run(adb_bin, serial, 'shell', f'blockdev --getsize64 {PART_USERDATA}'))
    buf = adb_exec_bytes(adb_bin, serial, f'dd if={PART_USERDATA} bs=4096 count=1 2>/dev/null')
    fs_bytes = parse_f2fs_size_bytes(buf)
    if fs_bytes is not None and 0 < fs_bytes <= part_bytes:
        print(f'Verified sda14 F2FS fits partition ({fs_bytes} <= {part_bytes} bytes)')
        return

    require(
        allow_format,
        f'sda14 userdata requires formatting after GPT shrink (or is unformatted; '
        f'fs_bytes={fs_bytes}, part_bytes={part_bytes}); re-run with --yes --format-userdata-if-needed',
    )
    print(f'Formatting shrunk sda14 userdata (fs_bytes={fs_bytes}, part_bytes={part_bytes})...')
    adb_run(adb_bin, serial, 'shell', 'umount /sdcard 2>/dev/null || true; umount /data 2>/dev/null || true')
    fmt_out = adb_run(adb_bin, serial, 'shell', 'twrp format data')
    print(f'twrp format data output:\n{fmt_out}')

    require(adb_run(adb_bin, serial, 'shell', 'readlink -f /dev/block/by-name/unixnu') == PART_UNIXNU,
            f'CRITICAL: by-name/unixnu changed after format')
    unixnu_bytes = int(adb_run(adb_bin, serial, 'shell', f'blockdev --getsize64 {PART_UNIXNU}'))
    require(unixnu_bytes == PART_BYTES,
            f'CRITICAL: sda15 size changed after format ({unixnu_bytes} != {PART_BYTES})')
    after_buf = adb_exec_bytes(adb_bin, serial, f'dd if={PART_USERDATA} bs=4096 count=1 2>/dev/null')
    after_fs_bytes = parse_f2fs_size_bytes(after_buf)
    if after_fs_bytes is not None:
        require(0 < after_fs_bytes <= part_bytes,
                f'sda14 F2FS still exceeds partition ({after_fs_bytes} > {part_bytes})')
        print(f'Verified sda14 F2FS after format ({after_fs_bytes} <= {part_bytes} bytes)')
    else:
        blkid_out = adb_run(adb_bin, serial, 'shell', f'blkid {PART_USERDATA} 2>/dev/null || true')
        require(len(after_buf) == 4096 and any(after_buf), f'sda14 appears empty after format (blkid={blkid_out})')
        print(f'Verified sda14 formatted ({blkid_out})')


def resolve_adb(explicit_adb):
    if explicit_adb:
        return explicit_adb
    candidate = Path.home() / 'Library/Android/sdk/platform-tools/adb'
    if candidate.is_file():
        return str(candidate)
    return 'adb'


def run_host_checks(args):
    checker = _load_module('check-aston-staged-bundle.py')
    staged = _load_module('flash-unixnu-staged.py')
    menu_mod = _load_module('aston-payload-menu.py')

    windows = [f'OS={args.os_dmg}', f'SystemOS={args.systemos_dmg}']
    objects = [
        f'OS.vroot={args.os_vroot}',
        f'SystemOS.vroot={args.systemos_vroot}',
        f'Payload={args.payload}',
        f'Wifi={args.wifi}',
    ]
    entries, total, version, manifest_mib = staged.build_layout(windows, objects)
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_bytes(manifest_mib)

    seed_info = checker.check_seed_geometry(args.seed) if args.seed else None
    boot_a_info = checker.check_boot_a(args.boot_a)
    payload_info = checker.check_payload_image(
        args.payload,
        menu_mod.loads_menu_toml,
        required_ids=['xnu', 'install', 'slot-b', 'recovery', 'fastboot', 'shutdown'],
        expected_default=args.expected_default,
    )
    return {
        'manifest_version': version,
        'total_bytes': total,
        'manifest_sha256': hashlib.sha256(manifest_mib[:4096]).hexdigest(),
        'manifest_mib_sha256': hashlib.sha256(manifest_mib).hexdigest(),
        'entries': [e['name'] for e in entries],
        'seed': seed_info,
        'boot_a': boot_a_info,
        'payload': payload_info,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--adb', help='path to adb (defaults to ~/Library/Android/sdk/platform-tools/adb or PATH)')
    parser.add_argument('--serial', default=DEFAULT_SERIAL)
    parser.add_argument('--boot-a', required=True, type=Path)
    parser.add_argument('--known-boot-a', type=Path, action='append', default=[])
    parser.add_argument('--boot-b-sha256', help='expected immutable SHA-256 of sde45 (boot_b)')
    parser.add_argument('--os-dmg', required=True, type=Path)
    parser.add_argument('--systemos-dmg', required=True, type=Path)
    parser.add_argument('--os-vroot', required=True, type=Path)
    parser.add_argument('--systemos-vroot', required=True, type=Path)
    parser.add_argument('--payload', required=True, type=Path)
    parser.add_argument('--wifi', required=True, type=Path)
    parser.add_argument('--seed', type=Path, help='optional data.dbr to double-check store start block 4456335')
    parser.add_argument('--expected-default', default=None, help='optional expected boot.default menu ID')
    parser.add_argument('--manifest-out', type=Path, help='optional path to write staged manifest binary')
    parser.add_argument('--check-only', action='store_true', help='run host verification only and exit without touching adb')
    parser.add_argument('--yes', action='store_true', help='pass --yes to flash-unixnu-staged.py and flash-aston-boot-a.py')
    parser.add_argument('--format-userdata-if-needed', action='store_true',
                        help='check sda14 userdata F2FS superblock bounds after GPT carve and run twrp format data if needed')
    parser.add_argument('--switch-to-slot-a', action='store_true',
                        help='only with --yes: run twrp/bootctl slot switch after full readback verification')
    args = parser.parse_args()

    summary = run_host_checks(args)
    print(json.dumps(summary, indent=2))
    if args.check_only:
        print('PASS: --check-only completed with all host bundle checks green')
        return

    adb_bin = resolve_adb(args.adb)
    require(adb_run(adb_bin, args.serial, 'get-state') == 'recovery', 'device not in TWRP recovery')
    require(adb_run(adb_bin, args.serial, 'shell', 'getprop ro.twrp.version') != '', 'not running TWRP')
    require(adb_run(adb_bin, args.serial, 'shell', 'getprop ro.boot.slot_suffix') == '_b', 'active slot is not _b')

    if args.boot_b_sha256:
        require(adb_run(adb_bin, args.serial, 'shell', 'readlink -f /dev/block/by-name/boot_b') == BOOT_B_PART,
                'by-name/boot_b does not resolve to sde45')
        got_b = adb_run(adb_bin, args.serial, 'shell', f'dd if={BOOT_B_PART} bs=1048576 2>/dev/null | sha256sum').split()[0]
        require(got_b == args.boot_b_sha256, f'boot_b precheck sha256={got_b}, expected {args.boot_b_sha256}')
        print(f'Verified boot_b untouched before write: {got_b}')

    if args.format_userdata_if_needed:
        ensure_userdata_f2fs_fits(adb_bin, args.serial, args.yes)

    stage_cmd = [
        sys.executable, str(SCRIPTS_DIR / 'flash-unixnu-staged.py'),
        '--adb', adb_bin,
        '--serial', args.serial,
        '--window', f'OS={args.os_dmg}',
        '--window', f'SystemOS={args.systemos_dmg}',
        '--object', f'OS.vroot={args.os_vroot}',
        '--object', f'SystemOS.vroot={args.systemos_vroot}',
        '--object', f'Payload={args.payload}',
        '--object', f'Wifi={args.wifi}',
        '--init-store-and-log',
    ]
    if args.manifest_out:
        stage_cmd.extend(['--manifest-out', str(args.manifest_out)])
    if args.yes:
        stage_cmd.append('--yes')
    subprocess.run(stage_cmd, check=True)

    require(args.serial == DEFAULT_SERIAL, 'flash-aston-boot-a.py only targets ed5f732f')
    boot_a_cmd = [sys.executable, str(SCRIPTS_DIR / 'flash-aston-boot-a.py'), '--adb', adb_bin]
    for known in args.known_boot_a:
        boot_a_cmd.extend(['--known', str(known)])
    if args.yes:
        boot_a_cmd.append('--yes')
    boot_a_cmd.append(str(args.boot_a))
    subprocess.run(boot_a_cmd, check=True)

    # Post-flash safety invariants
    if args.boot_b_sha256:
        after_b = adb_run(adb_bin, args.serial, 'shell', f'dd if={BOOT_B_PART} bs=1048576 2>/dev/null | sha256sum').split()[0]
        require(after_b == args.boot_b_sha256, f'CRITICAL: boot_b changed during flash: {after_b}')
        print(f'Verified boot_b untouched after write: {after_b}')

    root_sha = adb_run(adb_bin, args.serial, 'shell', f'dd if={PART_UNIXNU} bs=4096 count=1 2>/dev/null | sha256sum').split()[0]
    require(root_sha == summary['manifest_sha256'], f'SHNGROOT block 0 mismatch: {root_sha}')
    store_head_sha = adb_run(
        adb_bin, args.serial, 'shell',
        f'dd if={PART_UNIXNU} bs=4096 skip={STORE_START_BLOCK} count=512 2>/dev/null | sha256sum',
    ).split()[0]
    expected_zero_sha = hashlib.sha256(b'\x00' * (512 * 4096)).hexdigest()
    require(store_head_sha == expected_zero_sha, 'SHNGDATA header/bitmap head not zeroed cleanly')

    print('DONE: sda15 unixnu staged + sde13 boot_a flashed and verified; ColorOS 16 Slot B untouched')
    if args.switch_to_slot_a:
        require(args.yes, '--switch-to-slot-a requires --yes')
        slot_cmd = (
            'if command -v bootctl >/dev/null 2>&1; then '
            'bootctl set-active-boot-slot 0 && echo "SLOT_NOW=$(bootctl get-current-slot)"; '
            'else twrp setslot a; fi'
        )
        out = adb_run(adb_bin, args.serial, 'shell', slot_cmd)
        print(f'Slot switch output: {out}')
        require(
            'SLOT_NOW=0' in out or 'Slot_A' in out or 'slot a' in out.lower() or 'active boot slot to a' in out.lower(),
            f'Failed to confirm active slot A switch: {out}',
        )
    else:
        print('Next step: switch active slot to A (0) and reboot into UniXNU boot menu.')


if __name__ == '__main__':
    main()

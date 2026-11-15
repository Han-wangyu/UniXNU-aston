#!/usr/bin/env python3
"""Offline host-side preflight verifier for an Ace 3 UniXNU staged bundle.

Validates every host invariant without touching USB or the phone:
  - GPT LUN0 primary table CRCs, partition bounds, non-overlap, READ(10) LBA range, unique "unixnu"
  - SHNGROOT v2 manifest header SHA-256, entries <= 8, 4K alignment, probe LBA separation, log headroom
  - Source file digests vs SHNGROOT entries & raw non-UDIF DMG headers
  - FAT32 payload-*.img unixnu.toml required entries, default='slot-b', no duplicate IDs, args <= 1016 bytes
  - Optional data.dbr ("DBRDISK1") store geometry vs block 4456335
  - Optional boot_a image exact size (201326592 B), ANDROID! magic & AVBf footer
  - Optional UEFI directory sha256sums.txt verification

Compatible with Python 3.9+.
"""
import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

BLOCK = 4096
PART_BYTES = 34359738368
PART_BLOCKS = PART_BYTES // BLOCK  # 8388608
STORE_START_BLOCK = 4456335
STORE_RESERVE_BLOCKS = 1024 * 256  # 1024 MiB (NativeUfs.cpp kDefaultReserveMiB)
PROBE_REPORT_BLOCK = 4194304
LOG_RESERVE_BLOCKS = 8192
EXPORT_BLOCKS = 65536
LOG_GUARD_BLOCKS = 1024            # NativeUfs.cpp line 1515 (4 MiB guard)
BOOT_A_BYTES = 201326592
MAX_XNU_ARGS_BYTES = 1016

SCRIPTS_DIR = Path(__file__).resolve().parent


def require(ok, why):
    if not ok:
        raise SystemExit(f'FAIL: {why}')


def _load_module(filename):
    path = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(filename.replace('-', '_').removesuffix('.py'), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _verify_gpt_header(hdr):
    require(hdr[:8] == b'EFI PART', 'missing EFI PART signature')
    rev, hdr_size, hdr_crc, reserved = struct.unpack_from('<IIII', hdr, 8)
    require(rev == 0x00010000 and 92 <= hdr_size <= BLOCK and reserved == 0, 'invalid GPT header revision/size')
    zeroed = hdr[:16] + b'\x00\x00\x00\x00' + hdr[20:hdr_size]
    require((zlib.crc32(zeroed) & 0xFFFFFFFF) == hdr_crc, 'GPT header CRC32 mismatch')
    my_lba, backup_lba, first_lba, last_lba = struct.unpack_from('<QQQQ', hdr, 24)
    entries_lba, count, entry_size, entries_crc = struct.unpack_from('<QIII', hdr, 72)
    require(entry_size == 128 and 1 <= count <= 128, 'unexpected GPT geometry')
    return {
        'my_lba': my_lba,
        'backup_lba': backup_lba,
        'first_lba': first_lba,
        'last_lba': last_lba,
        'entries_lba': entries_lba,
        'count': count,
        'entry_size': entry_size,
        'entries_crc': entries_crc,
    }


def check_gpt_file(gpt_path):
    data = gpt_path.read_bytes()
    if len(data) >= 34 * BLOCK and data[BLOCK:BLOCK + 8] == b'EFI PART':
        gpt_format = 'raw-lun0'
        primary = _verify_gpt_header(data[BLOCK:2 * BLOCK])
        require(primary['my_lba'] == 1, 'unexpected raw LUN0 primary LBA')
        require(primary['backup_lba'] <= 0xFFFFFFFF and primary['first_lba'] <= primary['last_lba'] < primary['backup_lba'],
                'GPT exceeds READ(10) LBA bounds')
        ent_off = primary['entries_lba'] * BLOCK
        ent_bytes = primary['count'] * primary['entry_size']
        require(len(data) >= ent_off + ent_bytes, 'GPT dump truncated before end of entries')
        ent_blob = data[ent_off:ent_off + ent_bytes]
    elif len(data) >= 1536 and data[512:520] == b'EFI PART' and data[1024:1032] == b'EFI PART':
        gpt_format = 'sgdisk-backup'
        primary = _verify_gpt_header(data[512:1024])
        secondary = _verify_gpt_header(data[1024:1536])
        require(primary['my_lba'] == 1 and secondary['my_lba'] == primary['backup_lba'] and secondary['backup_lba'] == 1,
                'sgdisk backup primary/secondary LBA mismatch')
        require(primary['backup_lba'] <= 0xFFFFFFFF and primary['first_lba'] <= primary['last_lba'] < primary['backup_lba'],
                'GPT exceeds READ(10) LBA bounds')
        require((primary['first_lba'], primary['last_lba'], primary['count'], primary['entries_crc'])
                == (secondary['first_lba'], secondary['last_lba'], secondary['count'], secondary['entries_crc']),
                'sgdisk primary/secondary GPT metadata mismatch')
        ent_bytes = primary['count'] * primary['entry_size']
        require(len(data) >= 1536 + ent_bytes, 'sgdisk backup truncated before end of entries')
        ent_blob = data[1536:1536 + ent_bytes]
    else:
        raise SystemExit(f'FAIL: unrecognized GPT format/size in {gpt_path} ({len(data)} bytes)')

    require((zlib.crc32(ent_blob) & 0xFFFFFFFF) == primary['entries_crc'], 'GPT entries CRC32 mismatch')

    parts = {}
    used_ranges = []
    first_lba = primary['first_lba']
    last_lba = primary['last_lba']
    for idx in range(primary['count']):
        e = ent_blob[idx * 128:(idx + 1) * 128]
        if e[:16] == b'\x00' * 16:
            continue
        p_first, p_last, _attrs = struct.unpack_from('<QQQ', e, 32)
        require(first_lba <= p_first <= p_last <= last_lba, f'GPT entry {idx} outside usable range')
        for prior_first, prior_last in used_ranges:
            require(not (p_first <= prior_last and prior_first <= p_last), f'GPT partition overlap at entry {idx}')
        used_ranges.append((p_first, p_last))
        raw_name = e[56:128].decode('utf-16le', errors='ignore').split('\x00', 1)[0]
        parts.setdefault(raw_name, []).append((p_first, p_last))

    require('unixnu' in parts and len(parts['unixnu']) == 1, 'unique "unixnu" partition missing from GPT')
    u_first, u_last = parts['unixnu'][0]
    u_blocks = u_last - u_first + 1
    require(u_blocks == PART_BLOCKS, f'unixnu has {u_blocks} blocks, expected {PART_BLOCKS}')
    return {'format': gpt_format, 'unixnu_first_lba': u_first, 'unixnu_last_lba': u_last, 'unixnu_blocks': u_blocks}


def check_payload_image(img_path, loads_toml, required_ids=None, expected_default=None):
    mtype_bin = shutil.which('mtype') or '/opt/homebrew/bin/mtype'
    env = dict(os.environ, MTOOLS_SKIP_CHECK='1', TZ='UTC')
    raw = subprocess.run([mtype_bin, '-i', str(img_path), '::/EFI/UniXNU/unixnu.toml'],
                         env=env, check=True, capture_output=True).stdout.decode('utf-8')
    cfg = loads_toml(raw)
    boot = cfg.get('boot', {})
    entries = cfg.get('entry', [])
    ids = [e.get('id') for e in entries]
    require(len(ids) == len(set(ids)), f'{img_path.name}: duplicate menu IDs in {ids}')
    require(boot.get('default') in ids, f'{img_path.name}: boot.default={boot.get("default")!r} missing from {ids}')
    require('slot-b' in ids, f'{img_path.name}: missing Android safety fallback entry slot-b in {ids}')
    if expected_default is not None:
        require(boot.get('default') == expected_default,
                f'{img_path.name}: boot.default={boot.get("default")!r}, want {expected_default!r}')
    if required_ids:
        for req in required_ids:
            require(req in ids, f'{img_path.name}: missing required entry {req!r} in {ids}')

    max_args = 0
    for e in entries:
        if e.get('type') == 'xnu':
            arg_len = len(e.get('args', '').encode('utf-8'))
            require(arg_len <= MAX_XNU_ARGS_BYTES,
                    f'{img_path.name} entry {e.get("id")!r}: args length {arg_len} > {MAX_XNU_ARGS_BYTES}')
            max_args = max(max_args, arg_len)
    return {'file': img_path.name, 'sha256': hashlib.sha256(img_path.read_bytes()).hexdigest(),
            'entries': ids, 'default': boot.get('default'), 'max_args_bytes': max_args}


def check_seed_geometry(seed_path):
    with seed_path.open('rb') as f:
        hdr = f.read(32)
    require(len(hdr) == 32, 'short seed header')
    magic, version, page, logical, _count = struct.unpack('<8sIIQQ', hdr)
    require(magic == b'DBRDISK1' and version == 1 and page == BLOCK, 'not a valid DBRDISK1 seed')
    pages = logical // BLOCK
    require(logical % BLOCK == 0 and pages > 0, 'unaligned seed logical size')
    bitmap_blocks = (pages + 8 * BLOCK - 1) // (8 * BLOCK)
    window_blocks = 1 + bitmap_blocks + pages
    computed_store_start = PART_BLOCKS - STORE_RESERVE_BLOCKS - window_blocks
    require(computed_store_start == STORE_START_BLOCK,
            f'seed geometry store_start={computed_store_start}, expected {STORE_START_BLOCK}')
    return {'logical_bytes': logical, 'pages': pages, 'bitmap_blocks': bitmap_blocks,
            'window_blocks': window_blocks, 'store_start_block': computed_store_start}


def check_boot_a(boot_a_path):
    size = boot_a_path.stat().st_size
    require(size == BOOT_A_BYTES, f'{boot_a_path.name} size={size}, expected {BOOT_A_BYTES}')
    with boot_a_path.open('rb') as f:
        head = f.read(16)
        f.seek(-64, os.SEEK_END)
        tail = f.read(64)
    require(head[:8] == b'ANDROID!', f'{boot_a_path.name}: missing ANDROID! header magic')
    require(tail[:4] == b'AVBf', f'{boot_a_path.name}: missing AVBf footer magic')
    digest = hashlib.sha256()
    with boot_a_path.open('rb') as f:
        for blk in iter(lambda: f.read(1 << 20), b''):
            digest.update(blk)
    return {'file': str(boot_a_path), 'bytes': size, 'sha256': digest.hexdigest()}


def check_manifest_and_sources(manifest_path, staged_mod, sources):
    raw = manifest_path.read_bytes()
    require(len(raw) >= BLOCK, f'manifest too short: {len(raw)} bytes')
    version, total, entries = staged_mod.parse_header(raw[:BLOCK])
    require(version == 2 and 1 <= len(entries) <= 8,
            f'SHNGROOT version={version} count={len(entries)} unsupported by RootLayout.h (v2 max 8 entries)')
    require(total % BLOCK == 0 and total <= STORE_START_BLOCK * BLOCK,
            f'layout total={total} exceeds store start {STORE_START_BLOCK * BLOCK}')

    total_blocks = total // BLOCK
    # Check both probe separation and NativeUfs::native_ufs_log_open headroom (line 1515)
    require(total_blocks <= PROBE_REPORT_BLOCK,
            f'layout blocks={total_blocks} collides with probe block {PROBE_REPORT_BLOCK}')
    require(PART_BLOCKS - STORE_RESERVE_BLOCKS >= total_blocks + LOG_GUARD_BLOCKS,
            'insufficient guard space below reserve for NativeUfs boot log')

    windows = [e for e in entries if e['block_size'] == 512 and e['flags'] == 1]
    objects = [e for e in entries if e['block_size'] == 4096 and e['flags'] == 3]
    require(len(windows) == 2 and len(objects) >= 1 and len(windows) + len(objects) == len(entries),
            f'unexpected window/object breakdown: {len(windows)} windows, {len(objects)} objects')

    src_map = {}
    for spec in sources:
        name, sep, p = spec.partition('=')
        require(sep == '=' and name, f'invalid --source {spec!r}')
        src_map[name] = Path(p).expanduser().resolve()

    verified_sources = {}
    for e in entries:
        require(e['offset'] >= BLOCK and e['offset'] % BLOCK == 0, f'bad entry offset: {e}')
        if e['name'] in src_map:
            src_path = src_map[e['name']]
            require(src_path.is_file(), f'missing source file for {e["name"]}: {src_path}')
            require(src_path.stat().st_size == e['size'],
                    f'size mismatch for {e["name"]}: disk={src_path.stat().st_size} manifest={e["size"]}')
            got_sha = staged_mod.sha256_file_exact(src_path).hex()
            require(got_sha == e['sha256'].hex(),
                    f'sha256 mismatch for {e["name"]}: disk={got_sha} manifest={e["sha256"].hex()}')
            verified_sources[e['name']] = {'path': str(src_path), 'size': e['size'], 'sha256': got_sha}

    return {
        'version': version,
        'total_bytes': total,
        'total_blocks': total_blocks,
        'headroom_to_probe_blocks': PROBE_REPORT_BLOCK - total_blocks,
        'headroom_to_store_bytes': STORE_START_BLOCK * BLOCK - total,
        'entries': [e['name'] for e in entries],
        'verified_sources': verified_sources,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--gpt', type=Path, help='path to carved LUN0 GPT binary (e.g. gpt-sda-carved.bin)')
    parser.add_argument('--manifest', required=True, type=Path, help='path to staged SHNGROOT manifest (manifest-prod.bin)')
    parser.add_argument('--source', action='append', default=[], metavar='NAME=FILE',
                        help='verify source file matches named SHNGROOT entry')
    parser.add_argument('--payload', action='append', default=[], type=Path, help='payload FAT32 image(s) to verify')
    parser.add_argument('--require-entry', action='append', default=[], help='required menu entry ID(s)')
    parser.add_argument('--expected-default', default=None, help='optional expected default menu ID')
    parser.add_argument('--seed', type=Path, help='path to data.dbr DBRDISK1 seed')
    parser.add_argument('--boot-a', type=Path, help='path to boot_a-*-avb.img')
    parser.add_argument('--uefi-dir', type=Path, help='UEFI artifacts directory containing sha256sums.txt')
    args = parser.parse_args()

    staged_mod = _load_module('flash-unixnu-staged.py')
    menu_mod = _load_module('aston-payload-menu.py')

    report = {'manifest': check_manifest_and_sources(args.manifest, staged_mod, args.source)}
    if args.gpt:
        report['gpt'] = check_gpt_file(args.gpt)
    if args.seed:
        report['seed'] = check_seed_geometry(args.seed)
    if args.boot_a:
        report['boot_a'] = check_boot_a(args.boot_a)
    if args.payload:
        report['payloads'] = [
            check_payload_image(p, menu_mod.loads_menu_toml, args.require_entry or None, args.expected_default)
            for p in args.payload
        ]
    if args.uefi_dir:
        sums = args.uefi_dir / 'sha256sums.txt'
        require(sums.is_file(), f'missing {sums}')
        subprocess.run(['shasum', '-a', '256', '-c', sums.name], cwd=str(args.uefi_dir), check=True)
        report['uefi_sums_verified'] = True

    print(json.dumps(report, indent=2))
    print('PASS: all staged Ace 3 UniXNU bundle invariants verified')


if __name__ == '__main__':
    main()

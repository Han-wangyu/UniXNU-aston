#!/usr/bin/env python3
"""Read-only diagnostic puller for Ace 3's unixnu partition (/dev/block/sda15).

Fetches and decodes:
  1. SHNGROOT v1/v2 manifest at block 0 (checksum-checked)
  2. AstonProbe UEFI 4K report at block 4194304 (16 GiB offset, non-destructive)
  3. SHLOG001 kernel/user boot log at block 8380416 (header, msgbuf 1..32, latest slots)
  4. Optional export / diag blob from the ShengUfs export region at block 8314880

Works live over TWRP/rooted-Android adb, or offline against a local partition dump (--local-image).
Compatible with Python 3.9+.
"""
import argparse
import hashlib
import json
import os
import shlex
import struct
import subprocess
from pathlib import Path

DEFAULT_SERIAL = 'ed5f732f'
PART = '/dev/block/sda15'
PART_BYTES = 34359738368
BLOCK = 4096

SHNGROOT_MAGIC = b'SHNGROOT'
HEADER_HASH_OFFSET = 4064
ROOT_HDR_STRUCT = struct.Struct('<8sIIQ')
ROOT_ENTRY_STRUCT = struct.Struct('<32sQQII32s40s')

PROBE_REPORT_BLOCK = 4194304
EXPORT_START_BLOCK = 8314880
EXPORT_BLOCKS = 65536
BOOTLOG_START_BLOCK = 8380416
BOOTLOG_BLOCKS = 8192

SHLOG_MAGIC = b'SHLOG001'
# struct boot_log_header (package/BootLog.c lines 304-318)
SHLOG_HDR_STRUCT = struct.Struct('<8sIIQQ8I6I32s5I2I')


def require(ok, why):
    if not ok:
        raise SystemExit(f'STOP: {why}')


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


class PartitionReader:
    def __init__(self, adb_bin=None, serial=DEFAULT_SERIAL, android=False, local_image=None):
        self.adb_bin = adb_bin
        self.serial = serial
        self.android = android
        self.local_image = Path(local_image) if local_image else None

    def _adb(self, *cmd, binary=False):
        if self.android and cmd[0] in ('shell', 'exec-out'):
            cmd = (cmd[0], 'su -c ' + shlex.quote(' '.join(cmd[1:])))
        res = subprocess.run(
            [self.adb_bin, '-s', self.serial, *cmd],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,
        )
        return res.stdout if binary else res.stdout.decode('utf-8', errors='replace').strip()

    def verify_target(self):
        if self.local_image is not None:
            require(self.local_image.is_file(), f'missing local image: {self.local_image}')
            require(self.local_image.stat().st_size >= (PROBE_REPORT_BLOCK + 1) * BLOCK,
                    f'local image smaller than expected: {self.local_image.stat().st_size}')
            return
        if self.android:
            require(self._adb('get-state') == 'device', 'the phone is not in Android')
            require(self._adb('shell', 'id -u') == '0', 'su does not give root')
        else:
            require(self._adb('get-state') == 'recovery', 'the phone is not in recovery')
            require(self._adb('shell', 'getprop ro.twrp.version') != '', 'not TWRP')
        require(self._adb('shell', 'getprop ro.serialno') == self.serial, 'unexpected device serial')
        require(self._adb('shell', 'readlink -f /dev/block/by-name/unixnu') == PART, 'by-name/unixnu is not sda15')
        require('PARTNAME=unixnu' in self._adb('shell', 'cat /sys/class/block/sda15/uevent'), 'sda15 is not named unixnu')
        require(int(self._adb('shell', f'blockdev --getsize64 {PART}')) == PART_BYTES, 'unixnu size differs from 32 GiB')

    def read_blocks(self, start_block, count_blocks):
        want = count_blocks * BLOCK
        if self.local_image is not None:
            with self.local_image.open('rb') as f:
                f.seek(start_block * BLOCK)
                blob = f.read(want)
            return blob
        blob = self._adb('exec-out', f'dd if={PART} bs={BLOCK} skip={start_block} count={count_blocks} 2>/dev/null', binary=True)
        require(len(blob) == want, f'short read at block {start_block}: {len(blob)} of {want} bytes')
        there = self._adb('shell', f'dd if={PART} bs={BLOCK} skip={start_block} count={count_blocks} 2>/dev/null | sha256sum').split()[0]
        require(sha256_bytes(blob) == there, f'SHA-256 mismatch reading blocks {start_block}..{start_block + count_blocks - 1}')
        return blob


def decode_shngroot(block):
    if len(block) < BLOCK or block[:8] != SHNGROOT_MAGIC:
        return {'present': False, 'magic_hex': block[:8].hex()}
    magic, version, count, total = ROOT_HDR_STRUCT.unpack_from(block, 0)
    digest_ok = sha256_bytes(block[:HEADER_HASH_OFFSET]) == block[HEADER_HASH_OFFSET:BLOCK].hex()
    entries = []
    for idx in range(min(count, 31)):
        raw_name, offset, size, blk, flags, sha_b, _ = ROOT_ENTRY_STRUCT.unpack_from(block, 24 + 128 * idx)
        entries.append({
            'name': raw_name.split(b'\x00', 1)[0].decode('ascii', errors='replace'),
            'offset': offset,
            'size': size,
            'block_size': blk,
            'flags': flags,
            'sha256': sha_b.hex(),
        })
    return {
        'present': True,
        'magic': magic.decode('ascii'),
        'version': version,
        'count': count,
        'total_bytes': total,
        'header_checksum_ok': digest_ok,
        'entries': entries,
    }


def decode_probe_block(block):
    if block == b'\x00' * BLOCK:
        return False, ''
    raw_text = block.split(b'\x00', 1)[0].decode('utf-8', errors='replace').strip()
    if not raw_text:
        return True, f'<non-zero binary block, sha256={sha256_bytes(block)}>'
    return True, raw_text


def decode_shlog_header(block):
    if len(block) < SHLOG_HDR_STRUCT.size or block[:8] != SHLOG_MAGIC:
        return None
    fields = SHLOG_HDR_STRUCT.unpack_from(block, 0)
    (
        magic, version, header_bytes, boot_id, first_lba,
        window_blocks, msg_first, msg_blocks, msg_bytes,
        slot_first, slot_blocks, slot_count, slots_written,
        uptime_s, rounds, write_errors, gpt_open, gpt_last, state,
        raw_tag,
        export_first, export_blocks, export_bytes, export_generation, export_below,
        diag_first, diag_bytes,
    ) = fields
    return {
        'magic': magic.decode('ascii'),
        'version': version,
        'header_bytes': header_bytes,
        'boot_id': boot_id,
        'first_lba': first_lba,
        'window_blocks': window_blocks,
        'msg_first': msg_first,
        'msg_blocks': msg_blocks,
        'msg_bytes': msg_bytes,
        'slot_first': slot_first,
        'slot_blocks': slot_blocks,
        'slot_count': slot_count,
        'slots_written': slots_written,
        'uptime_s': uptime_s,
        'rounds': rounds,
        'write_errors': write_errors,
        'gpt_open': gpt_open,
        'gpt_last': gpt_last,
        'state': state,
        'tag': raw_tag.split(b'\x00', 1)[0].decode('utf-8', errors='replace'),
        'export_first': export_first,
        'export_blocks': export_blocks,
        'export_bytes': export_bytes,
        'export_generation': export_generation,
        'export_below': export_below,
        'diag_first': diag_first,
        'diag_bytes': diag_bytes,
    }


def extract_cstr_block(blob):
    return blob.split(b'\x00', 1)[0].decode('utf-8', errors='replace')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', default=DEFAULT_SERIAL)
    parser.add_argument('--android', action='store_true', help='read via su from rooted Android instead of TWRP')
    parser.add_argument('--local-image', type=Path, help='offline test mode: read from a local raw image/file')
    parser.add_argument('--out', required=True, type=Path, help='directory to save decoded probe/logs')
    parser.add_argument('--slots', type=int, default=4, help='number of most recent SHLOG001 slots to pull (default 4)')
    parser.add_argument('--pull-export', action='store_true', help='also pull non-empty export/diag blobs')
    args = parser.parse_args()

    reader = PartitionReader(args.adb, args.serial, args.android, args.local_image)
    reader.verify_target()
    args.out.mkdir(parents=True, exist_ok=True)

    root_info = decode_shngroot(reader.read_blocks(0, 1))
    (args.out / 'shngroot.json').write_bytes((json.dumps(root_info, indent=2) + '\n').encode('utf-8'))
    print(f"SHNGROOT: present={root_info['present']}"
          + (f" v{root_info['version']} entries={root_info['count']} ok={root_info['header_checksum_ok']}"
             if root_info['present'] else ''))

    probe_blk = reader.read_blocks(PROBE_REPORT_BLOCK, 1)
    probe_present, probe_text = decode_probe_block(probe_blk)
    (args.out / 'aston-probe.bin').write_bytes(probe_blk)
    (args.out / 'aston-probe.txt').write_bytes((probe_text + '\n').encode('utf-8'))
    print(f'AstonProbe @{PROBE_REPORT_BLOCK}: present={probe_present}')
    if probe_present:
        print(probe_text[:500])

    log_hdr_raw = reader.read_blocks(BOOTLOG_START_BLOCK, 1)
    log_hdr = decode_shlog_header(log_hdr_raw)
    if log_hdr is None:
        summary = {'shngroot': root_info, 'probe_present': probe_present, 'bootlog_present': False}
        (args.out / 'summary.json').write_bytes((json.dumps(summary, indent=2) + '\n').encode('utf-8'))
        print(f'SHLOG001 @{BOOTLOG_START_BLOCK}: not present (clean or not yet booted with sheng-log=1)')
        return

    (args.out / 'bootlog-header.json').write_bytes((json.dumps(log_hdr, indent=2) + '\n').encode('utf-8'))
    print(f"SHLOG001 @{BOOTLOG_START_BLOCK}: state={log_hdr['state']} rounds={log_hdr['rounds']} "
          f"uptime={log_hdr['uptime_s']}s slots={log_hdr['slots_written']}/{log_hdr['slot_count']} "
          f"errors={log_hdr['write_errors']} tag={log_hdr['tag']!r}")

    if 0 < log_hdr['msg_blocks'] <= 128:
        msg_raw = reader.read_blocks(BOOTLOG_START_BLOCK + log_hdr['msg_first'], log_hdr['msg_blocks'])
        limit = min(log_hdr['msg_bytes'], len(msg_raw)) if log_hdr['msg_bytes'] else len(msg_raw)
        msg_text = msg_raw[:limit].rstrip(b'\x00').decode('utf-8', errors='replace')
        (args.out / 'msgbuf.txt').write_bytes((msg_text + '\n').encode('utf-8'))
        print(f"Saved msgbuf ({len(msg_text)} chars) -> {args.out / 'msgbuf.txt'}")

    written = log_hdr['slots_written']
    slot_count = log_hdr['slot_count']
    slot_blocks = log_hdr['slot_blocks']
    if written > 0 and slot_count > 0 and 0 < slot_blocks <= 64 and args.slots > 0:
        to_pull = min(args.slots, written, slot_count)
        slots_dir = args.out / 'slots'
        slots_dir.mkdir(exist_ok=True)
        latest_text = ''
        for offset_back in range(to_pull - 1, -1, -1):
            slot_idx = written - 1 - offset_back
            ring_pos = slot_idx % slot_count
            rel_blk = log_hdr['slot_first'] + ring_pos * slot_blocks
            require(rel_blk + slot_blocks <= BOOTLOG_BLOCKS, f'slot block out of bounds: {rel_blk}')
            text = extract_cstr_block(reader.read_blocks(BOOTLOG_START_BLOCK + rel_blk, slot_blocks))
            (slots_dir / f'slot-{slot_idx:05d}.txt').write_bytes((text + '\n').encode('utf-8'))
            latest_text = text
        if latest_text:
            (args.out / 'slot-latest.txt').write_bytes((latest_text + '\n').encode('utf-8'))
            print(f"Saved {to_pull} slot(s); latest -> {args.out / 'slot-latest.txt'}")

    if args.pull_export:
        export_base = BOOTLOG_START_BLOCK - (log_hdr['export_below'] or EXPORT_BLOCKS)
        for label, rel, size_bytes in (
            ('export', log_hdr['export_first'], log_hdr['export_bytes']),
            ('diag', log_hdr['diag_first'], log_hdr['diag_bytes']),
        ):
            if size_bytes > 0:
                n_blks = (size_bytes + BLOCK - 1) // BLOCK
                require(rel + n_blks <= (log_hdr['export_below'] or EXPORT_BLOCKS), f'{label} export out of bounds')
                blob = reader.read_blocks(export_base + rel, n_blks)[:size_bytes]
                out_path = args.out / f'{label}.bin'
                out_path.write_bytes(blob)
                print(f'Saved {label} blob ({size_bytes} bytes) -> {out_path}')

    summary = {'shngroot': root_info, 'probe_present': probe_present, 'bootlog_present': True, 'bootlog': log_hdr}
    (args.out / 'summary.json').write_bytes((json.dumps(summary, indent=2) + '\n').encode('utf-8'))


if __name__ == '__main__':
    main()

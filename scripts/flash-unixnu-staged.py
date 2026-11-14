#!/usr/bin/env python3
"""Streams a UniXNU SHNGROOT v2 userdata layout directly onto the Ace 3's unixnu partition (sda15) via TWRP adb.

Unlike `unixnu build userdata` + `flash-unixnu-image.py`, this tool never materializes a 16+ GiB combined image on the
host PC. It hashes each window and data object directly, builds the 4096-byte SHNGROOT v2 header (zero-padded to 1 MiB),
streams every 128 MiB data piece above offset 1 MiB over `adb push` + verified `sha256sum` + `dd ... conv=notrunc,fsync`
(skipping chunks already matching on-device), writes the 1 MiB SHNGROOT manifest at offset 0 LAST, and verifies full
on-device SHA-256 readback before returning.

Usage:
  flash-unixnu-staged.py [--dry-run] [--manifest-out PATH] \\
    --window OS=/path/to/OS.dmg --window SystemOS=/path/to/SystemOS.dmg \\
    --object OS.vroot=/path/to/OS.vroot --object SystemOS.vroot=/path/to/SystemOS.vroot \\
    --object Payload=/path/to/payload.img --object Wifi=/path/to/wifi.obj \\
    [--adb /path/to/adb] [--serial ed5f732f] [--update-payload-only] [--yes]
"""
import argparse
import hashlib
import json
import os
import struct
import subprocess
import tempfile
from pathlib import Path

MIB = 1 << 20
CHUNK = 128 * MIB
HASH_BUF = 8 * MIB
ZERO_MIB = b'\x00' * MIB

DEFAULT_SERIAL = 'ed5f732f'
PART = '/dev/block/sda15'
USERDATA_PART = '/dev/block/sda14'
PART_BYTES = 34359738368              # 8388608 * 4096
PART_START_BYTES = 115424000 * 4096
STORE_START_BYTES = 4456335 * 4096    # SHNGDATA store header at block 4456335

SHNGROOT_MAGIC = b'SHNGROOT'
HEADER_SIZE = 4096
HEADER_HASH_OFFSET = 4064
HEADER_STRUCT = struct.Struct('<8sIIQ')
ENTRY_STRUCT = struct.Struct('<32sQQII32s40s')
MAX_ENTRIES = 31


def require(ok, why):
    if not ok:
        raise SystemExit(f'STOP: {why}; nothing written')


def parse_kv(spec):
    name, sep, path_str = spec.partition('=')
    require(sep == '=' and name and len(name.encode()) <= 31, f'invalid NAME=FILE argument: {spec!r}')
    path = Path(path_str).expanduser().resolve()
    require(path.is_file(), f'missing file for {name}: {path}')
    size = path.stat().st_size
    require(size > 0, f'empty file for {name}: {path}')
    if size >= 512:
        with path.open('rb') as f:
            f.seek(-512, os.SEEK_END)
            require(f.read(4) != b'koly', f'{path.name} is a compressed UDIF DMG; decrypt/extract to raw disk first')
    return name, path, size


def sha256_file_exact(path):
    digest = hashlib.sha256()
    with path.open('rb') as f:
        while chunk := f.read(HASH_BUF):
            digest.update(chunk)
    return digest.digest()


def build_layout(windows_spec, objects_spec):
    windows = [parse_kv(s) for s in windows_spec]
    objects = [parse_kv(s) for s in objects_spec]
    require(1 <= len(windows) <= 4, f'expected 1..4 windows, got {len(windows)}')
    require(len(objects) <= 4, f'expected 0..4 objects, got {len(objects)}')
    names = [n for n, _, _ in windows] + [n for n, _, _ in objects]
    require(len(names) == len(set(names)), f'duplicate entry names in {names}')

    cursor = MIB
    entries = []
    for kind, items in (('window', windows), ('object', objects)):
        block_size, flags = (512, 1) if kind == 'window' else (4096, 3)
        for name, path, size in items:
            aligned = (size + MIB - 1) // MIB * MIB
            entries.append({
                'kind': kind,
                'name': name,
                'path': path,
                'offset': cursor,
                'size': size,
                'aligned_bytes': aligned,
                'block_size': block_size,
                'flags': flags,
                'sha256': sha256_file_exact(path),
            })
            cursor += aligned

    total = cursor
    require(total <= STORE_START_BYTES,
            f'layout ends at {total} bytes, overlapping SHNGDATA store at {STORE_START_BYTES}')
    require(len(entries) <= MAX_ENTRIES, 'too many manifest entries')

    header = bytearray(HEADER_SIZE)
    version = 2 if objects else 1
    HEADER_STRUCT.pack_into(header, 0, SHNGROOT_MAGIC, version, len(entries), total)
    for idx, entry in enumerate(entries):
        ENTRY_STRUCT.pack_into(
            header, 24 + 128 * idx,
            entry['name'].encode(), entry['offset'], entry['size'],
            entry['block_size'], entry['flags'], entry['sha256'], bytes(40),
        )
    header[HEADER_HASH_OFFSET:HEADER_SIZE] = hashlib.sha256(bytes(header[:HEADER_HASH_OFFSET])).digest()
    manifest_mib = bytes(header) + b'\x00' * (MIB - HEADER_SIZE)
    return entries, total, version, manifest_mib


def parse_header(raw_header):
    require(len(raw_header) >= HEADER_SIZE, 'short SHNGROOT header')
    magic, version, count, total = HEADER_STRUCT.unpack_from(raw_header, 0)
    require(magic == SHNGROOT_MAGIC, f'invalid SHNGROOT magic {magic!r}')
    require(version in (1, 2) and count <= MAX_ENTRIES, f'invalid SHNGROOT version={version} count={count}')
    expected_digest = hashlib.sha256(raw_header[:HEADER_HASH_OFFSET]).digest()
    require(raw_header[HEADER_HASH_OFFSET:HEADER_SIZE] == expected_digest, 'SHNGROOT header checksum mismatch')
    entries = []
    for idx in range(count):
        raw_name, offset, size, block_size, flags, sha_bytes, _ = ENTRY_STRUCT.unpack_from(raw_header, 24 + 128 * idx)
        name = raw_name.split(b'\x00', 1)[0].decode('ascii')
        entries.append({
            'name': name,
            'offset': offset,
            'size': size,
            'block_size': block_size,
            'flags': flags,
            'sha256': sha_bytes,
        })
    return version, total, entries


class AdbPhone:
    def __init__(self, adb_bin, serial):
        self.adb_bin = adb_bin
        self.serial = serial

    def run(self, *cmd, binary=False):
        res = subprocess.run([self.adb_bin, '-s', self.serial, *cmd],
                             capture_output=True, check=True, stdin=subprocess.DEVNULL)
        return res.stdout if binary else res.stdout.decode().strip()

    def shell(self, command):
        return self.run('shell', command)

    def verify_twrp_target(self):
        require(self.run('get-state') == 'recovery', 'the phone is not in recovery')
        require(self.shell('getprop ro.twrp.version') != '', 'not TWRP')
        require(self.shell('getprop ro.serialno') == self.serial, 'wrong device serialno')
        require(self.shell('readlink -f /dev/block/by-name/unixnu') == PART, 'by-name/unixnu is not sda15')
        require(self.shell('readlink -f /dev/block/by-name/userdata') == USERDATA_PART, 'userdata is not sda14')
        require('PARTNAME=unixnu' in self.shell('cat /sys/class/block/sda15/uevent'), 'sda15 is not named unixnu')
        require(int(self.shell(f'blockdev --getsize64 {PART}')) == PART_BYTES, 'unixnu size differs from 32 GiB')
        require(int(self.shell('cat /sys/class/block/sda15/start')) * 512 == PART_START_BYTES, 'sda15 start differs')
        require(self.shell(f'grep -c "^{PART} " /proc/mounts || true') in ('0', ''), 'something on sda15 is mounted')

    def region_sha256(self, skip_mib, count_mib):
        if count_mib == 0:
            return hashlib.sha256().hexdigest()
        cmd = f'dd if={PART} bs=1048576 skip={skip_mib} count={count_mib} 2>/dev/null | sha256sum'
        return self.shell(cmd).split()[0]

    def write_piece_checked(self, local_path, offset_bytes, length_bytes, expected_sha):
        require(offset_bytes % MIB == 0 and length_bytes % MIB == 0, 'unaligned piece write')
        skip_mib = offset_bytes // MIB
        count_mib = length_bytes // MIB
        if self.region_sha256(skip_mib, count_mib) == expected_sha:
            print(f'unchanged {skip_mib}..{skip_mib + count_mib} MiB ({expected_sha[:12]})', flush=True)
            return
        remote = '/tmp/unixnu-chunk.bin'
        free = int(self.shell("df -k /tmp | tail -1 | awk '{print $4}'")) * 1024
        require(free >= length_bytes + 64 * MIB, f'TWRP /tmp free {free >> 20} MiB < needed {(length_bytes >> 20) + 64} MiB')
        self.run('push', str(local_path), remote)
        try:
            remote_sha = self.shell(f'sha256sum {remote}').split()[0]
            require(remote_sha == expected_sha, f'push corruption at {skip_mib} MiB: got {remote_sha}, want {expected_sha}')
            self.shell(f'dd if={remote} of={PART} bs=1048576 seek={skip_mib} count={count_mib} conv=notrunc,fsync 2>&1')
        finally:
            self.shell(f'rm -f {remote}')
        print(f'written   {skip_mib}..{skip_mib + count_mib} MiB ({expected_sha[:12]})', flush=True)


def stream_entry_chunks(phone, entry, tmpdir):
    piece_path = tmpdir / 'piece.bin'
    padded_digest = hashlib.sha256()
    with entry['path'].open('rb') as src:
        rel = 0
        while rel < entry['aligned_bytes']:
            piece_len = min(CHUNK, entry['aligned_bytes'] - rel)
            valid_len = max(0, min(entry['size'] - rel, piece_len))
            piece_hash = hashlib.sha256()
            with piece_path.open('wb') as out:
                remaining = valid_len
                while remaining > 0:
                    buf = src.read(min(HASH_BUF, remaining))
                    require(len(buf) > 0, f'unexpected EOF reading {entry["path"]}')
                    out.write(buf)
                    piece_hash.update(buf)
                    padded_digest.update(buf)
                    remaining -= len(buf)
                pad = piece_len - valid_len
                while pad > 0:
                    zeros = ZERO_MIB if pad >= MIB else b'\x00' * pad
                    out.write(zeros)
                    piece_hash.update(zeros)
                    padded_digest.update(zeros)
                    pad -= len(zeros)
            phone.write_piece_checked(piece_path, entry['offset'] + rel, piece_len, piece_hash.hexdigest())
            piece_path.unlink(missing_ok=True)
            rel += piece_len
    return padded_digest.hexdigest()


def verify_update_payload_only(phone, entries, total):
    raw = phone.run('exec-out', f'dd if={PART} bs=4096 count=1 2>/dev/null', binary=True)
    _, cur_total, cur_entries = parse_header(raw)
    cur_map = {e['name']: e for e in cur_entries}
    new_payload = [e for e in entries if e['name'] == 'Payload']
    require(len(new_payload) == 1, '--update-payload-only requires --object Payload=...')
    require('Payload' in cur_map, 'existing SHNGROOT on phone has no Payload entry')
    require([e['name'] for e in cur_entries] == [e['name'] for e in entries], 'entry order mismatch for payload update')
    for entry in entries:
        cur = cur_map[entry['name']]
        if entry['name'] == 'Payload':
            is_last = (entry['name'] == entries[-1]['name'])
            require(cur['offset'] == entry['offset'] and (cur['size'] == entry['size'] or is_last),
                    'Payload size/offset layout differs while non-final')
        else:
            require(cur['offset'] == entry['offset'] and cur['size'] == entry['size']
                    and cur['block_size'] == entry['block_size'] and cur['flags'] == entry['flags']
                    and cur['sha256'] == entry['sha256'],
                    f'non-Payload entry {entry["name"]} differs from device SHNGROOT')
    require(total <= STORE_START_BYTES and cur_total <= STORE_START_BYTES, 'layout overlaps SHNGDATA store')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial', default=DEFAULT_SERIAL)
    parser.add_argument('--window', action='append', default=[], metavar='NAME=FILE')
    parser.add_argument('--object', action='append', default=[], metavar='NAME=FILE')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--manifest-out', type=Path)
    parser.add_argument('--update-payload-only', action='store_true')
    parser.add_argument('--yes', action='store_true')
    args = parser.parse_args()

    entries, total, version, manifest_mib = build_layout(args.window, args.object)
    summary = {
        'part': PART,
        'serial': args.serial,
        'version': version,
        'total_bytes': total,
        'total_mib': total // MIB,
        'store_start_bytes': STORE_START_BYTES,
        'headroom_bytes': STORE_START_BYTES - total,
        'manifest_sha256': hashlib.sha256(manifest_mib[:HEADER_SIZE]).hexdigest(),
        'manifest_mib_sha256': hashlib.sha256(manifest_mib).hexdigest(),
        'entries': [
            {
                'kind': e['kind'],
                'name': e['name'],
                'offset': e['offset'],
                'size': e['size'],
                'aligned_bytes': e['aligned_bytes'],
                'sha256': e['sha256'].hex(),
                'source': str(e['path']),
            }
            for e in entries
        ],
    }
    if args.manifest_out:
        args.manifest_out.parent.mkdir(parents=True, exist_ok=True)
        args.manifest_out.write_bytes(manifest_mib)
    print(json.dumps(summary, indent=2))
    if args.dry_run:
        return

    phone = AdbPhone(args.adb, args.serial)
    phone.verify_twrp_target()
    if args.update_payload_only:
        verify_update_payload_only(phone, entries, total)

    confirm = f'{args.serial} unixnu {"payload-update" if args.update_payload_only else "staged-write"}'
    if not args.yes:
        require(input(f'Type exactly: {confirm}\n> ') == confirm, 'confirmation did not match')

    with tempfile.TemporaryDirectory(prefix='unixnu-stage-') as tmp:
        tmpdir = Path(tmp)
        written_padded = {}
        target_entries = [e for e in entries if e['name'] == 'Payload'] if args.update_payload_only else entries
        for entry in target_entries:
            print(f'==> Streaming {entry["kind"]} {entry["name"]} ({entry["size"]} B @ {entry["offset"] // MIB} MiB)',
                  flush=True)
            entry_padded_sha = stream_entry_chunks(phone, entry, tmpdir)
            readback = phone.region_sha256(entry['offset'] // MIB, entry['aligned_bytes'] // MIB)
            require(readback == entry_padded_sha,
                    f'readback mismatch for {entry["name"]}: phone={readback} expected={entry_padded_sha}')
            written_padded[entry['name']] = entry_padded_sha

        manifest_file = tmpdir / 'manifest.bin'
        manifest_file.write_bytes(manifest_mib)
        manifest_sha = hashlib.sha256(manifest_mib).hexdigest()
        print('==> Writing 1 MiB SHNGROOT manifest at offset 0 (LAST)', flush=True)
        phone.write_piece_checked(manifest_file, 0, MIB, manifest_sha)

    phone.shell('sync; echo 3 > /proc/sys/vm/drop_caches')
    require(phone.region_sha256(0, 1) == summary['manifest_mib_sha256'], 'final manifest readback mismatch')
    for entry in target_entries:
        skip_mib = entry['offset'] // MIB
        count_mib = entry['aligned_bytes'] // MIB
        got = phone.region_sha256(skip_mib, count_mib)
        require(got == written_padded[entry['name']],
                f'post-drop_caches mismatch on {entry["name"]}: got {got}, want {written_padded[entry["name"]]}')
    print(f'DONE: verified SHNGROOT v{version} ({total} B) on {PART}')


if __name__ == '__main__':
    main()

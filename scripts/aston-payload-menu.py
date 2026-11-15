#!/usr/bin/env python3
"""Rewrites the menu of a `unixnu build payload` volume for the Ace 3 (in place, with mtools).

On this phone the payload volume is the only configuration UniXNU sees when it is present (Ace3-UniXNU efi/AstonConfigDxe
publishes it instead of the firmware's own menu), so it must keep the way back to Android: the generated menu boots iOS by
default after 3 s and has no "Switch to slot B". The new menu keeps the generated iOS entries unchanged (kernel, initrd,
arguments, the install confirmation) and adds the firmware's slot switch, which is the default after 10 s.

--variant 'ID|TITLE|EDITS' adds an entry after "xnu" that boots the same kernel and Data with changed arguments, so one image
can compare settings on the device. EDITS are space separated: KEY=VALUE sets (or replaces) KEY, !KEY drops it (and must name
an argument the generated entry has). Nothing else of the entry changes.

usage: aston-payload-menu.py payload.img [--timeout 10] [--default slot-b] [--variant 'ID|TITLE|EDITS' ...]
Prints the SHA-256 of the rewritten volume; writes payload.img.menu.json beside it.
"""
import argparse
import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None

CONFIG = '::/EFI/UniXNU/unixnu.toml'
# Ace3-UniXNU efi/AstonSlotSwitch (FILE_GUID), resolved by UniXNU from the firmware volume.
SLOT_SWITCH = 'fv:7C4F0B9E-3D2A-4B61-9E85-2F6A1C0D7B43'
# Ace3-UniXNU efi/AstonRebootRecovery (FILE_GUID), switches back to slot B + BCB boot-recovery.
RECOVERY_SWITCH = 'fv:8D5E1C2A-4F3B-4E72-A9C6-3B7F2D1E8A54'
# AstonPkg/Applications/AstonRebootFastboot (FILE_GUID), reboots straight to Qualcomm fastboot.
FASTBOOT_SWITCH = 'fv:9E6F2D3B-5A4C-4F83-B8D7-4C8E3F2A9B65'
KEEP_TYPES = ('xnu',)


def _strip_toml_comment(line):
    in_dq = False
    in_sq = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_dq:
            if ch == '\\':
                escaped = True
            elif ch == '"':
                in_dq = False
        elif in_sq:
            if ch == "'":
                in_sq = False
        else:
            if ch == '"':
                in_dq = True
            elif ch == "'":
                in_sq = True
            elif ch == '#':
                return line[:idx]
    return line


def _parse_toml_value(raw):
    raw = raw.strip()
    if raw == 'true':
        return True
    if raw == 'false':
        return False
    if raw.startswith('"') and raw.endswith('"'):
        return json.loads(raw)
    if raw.startswith("'") and raw.endswith("'") and len(raw) >= 2:
        return raw[1:-1]
    if raw.startswith('[') and raw.endswith(']'):
        inner = raw[1:-1].strip()
        if not inner:
            return []
        items = []
        cur = []
        in_dq = False
        in_sq = False
        escaped = False
        for ch in inner:
            if escaped:
                cur.append(ch)
                escaped = False
            elif in_dq:
                cur.append(ch)
                if ch == '\\':
                    escaped = True
                elif ch == '"':
                    in_dq = False
            elif in_sq:
                cur.append(ch)
                if ch == "'":
                    in_sq = False
            else:
                if ch == '"':
                    in_dq = True
                    cur.append(ch)
                elif ch == "'":
                    in_sq = True
                    cur.append(ch)
                elif ch == ',':
                    part = ''.join(cur).strip()
                    if part:
                        items.append(_parse_toml_value(part))
                    cur = []
                else:
                    cur.append(ch)
        tail = ''.join(cur).strip()
        if tail:
            items.append(_parse_toml_value(tail))
        return items
    return int(raw, 0)


def loads_menu_toml(text):
    if tomllib is not None:
        return tomllib.loads(text)
    root = {}
    target = root
    for raw_line in text.splitlines():
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith('[[') and line.endswith(']]'):
            name = line[2:-2].strip()
            target = {}
            root.setdefault(name, []).append(target)
            continue
        if line.startswith('[') and line.endswith(']'):
            name = line[1:-1].strip()
            target = root.setdefault(name, {})
            continue
        key, sep, val = line.partition('=')
        if not sep:
            raise ValueError(f'invalid TOML line: {line!r}')
        target[key.strip()] = _parse_toml_value(val)
    return root


def value(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    if isinstance(v, int):
        return str(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, list) and all(isinstance(x, str) for x in v):
        return '[' + ', '.join(json.dumps(x, ensure_ascii=False) for x in v) + ']'
    raise SystemExit(f'unsupported value in the generated menu: {v!r}')


def table(name, items):
    lines = [f'[{name}]']
    for key, v in items.items():
        if isinstance(v, dict):
            raise SystemExit(f'nested table {name}.{key} in the generated menu')
        lines.append(f'{key} = {value(v)}')
    return '\n'.join(lines) + '\n'


def edit_args(line, edits):
    words = line.split()
    for edit in edits.split():
        if edit.startswith('!'):
            key = edit[1:]
            if not any(w.split('=', 1)[0] == key for w in words):
                raise SystemExit(f'a variant drops {key}, which the generated arguments do not have')
            words = [w for w in words if w.split('=', 1)[0] != key]
        else:
            key = edit.split('=', 1)[0]
            if any(w.split('=', 1)[0] == key for w in words):
                words = [edit if w.split('=', 1)[0] == key else w for w in words]
            else:
                words.append(edit)
    return ' '.join(words)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('image', type=Path)
    parser.add_argument('--timeout', type=int, default=10)
    parser.add_argument('--default', default='slot-b')
    parser.add_argument('--variant', action='append', default=[], metavar="'ID|TITLE|EDITS'")
    parser.add_argument('--drop-variants', action='store_true',
                        help='keep only xnu and install from existing xnu entries before applying --variant')
    args = parser.parse_args()
    env = dict(os.environ, MTOOLS_SKIP_CHECK='1', TZ='UTC')
    text = subprocess.run(['mtype', '-i', str(args.image), CONFIG], env=env, check=True, capture_output=True).stdout
    generated = loads_menu_toml(text.decode())
    entries = [e for e in generated.get('entry', []) if e.get('type') in KEEP_TYPES]
    if args.drop_variants:
        entries = [e for e in entries if e.get('id') in ('xnu', 'install')]
    if not entries or not any(e.get('id') == 'xnu' for e in entries):
        raise SystemExit('the generated menu has no xnu entry: not a `unixnu build payload` volume')
    for key in generated:
        if key not in ('version', 'boot', 'kits', 'entry', 'ui'):
            raise SystemExit(f'unexpected section [{key}] in the generated menu: review this script first')
    boot = dict(generated.get('boot', {}))
    boot.update(timeout=args.timeout, default=args.default, rotate=0)
    out = f'version = {value(generated.get("version", 1))}\n\n'
    out += '# Rewritten by UniXNU-aston scripts/aston-payload-menu.py: the way back to Android stays the default.\n'
    out += table('boot', boot) + '\n'
    if 'ui' in generated:
        out += table('ui', generated['ui']) + '\n'
    out += table('kits', generated.get('kits', {'autoload': False})) + '\n'
    for entry in entries:
        out += table('[entry]', entry) + '\n'
        if entry.get('id') == 'xnu':
            for spec in args.variant:
                vid, title, edits = spec.split('|', 2)
                out += table('[entry]', dict(entry, id=vid, title=title, args=edit_args(entry['args'], edits))) + '\n'
    out += table('[entry]', {'id': 'slot-b', 'title': 'Switch to slot B', 'type': 'efi', 'path': SLOT_SWITCH}) + '\n'
    out += table('[entry]', {'id': 'recovery', 'title': 'Reboot to TWRP Recovery', 'type': 'efi', 'path': RECOVERY_SWITCH}) + '\n'
    out += table('[entry]', {'id': 'fastboot', 'title': 'Reboot to Fastboot', 'type': 'efi', 'path': FASTBOOT_SWITCH}) + '\n'
    out += table('[entry]', {'id': 'shutdown', 'title': 'Power off', 'type': 'action', 'action': 'shutdown'})
    check = loads_menu_toml(out)
    ids = [e['id'] for e in check['entry']]
    assert check['boot']['default'] in ids and len(set(ids)) == len(ids)
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / 'unixnu.toml'
        path.write_bytes(out.encode('utf-8'))
        subprocess.run(['mcopy', '-o', '-m', '-i', str(args.image), str(path), CONFIG], env=env, check=True, capture_output=True)
    back = subprocess.run(['mtype', '-i', str(args.image), CONFIG], env=env, check=True, capture_output=True).stdout.decode()
    assert back == out, 'readback of the rewritten menu differs'
    digest = hashlib.sha256(args.image.read_bytes()).hexdigest()
    report = {'image': args.image.name, 'sha256': digest, 'bytes': args.image.stat().st_size, 'entries': ids,
              'default': args.default, 'timeout': args.timeout, 'generated_entries': [e.get('id') for e in generated.get('entry', [])]}
    Path(str(args.image) + '.menu.json').write_bytes((json.dumps(report, indent=2) + '\n').encode('utf-8'))
    print(out)
    print(f'menu rewritten: {ids}, default {args.default}; payload sha256 {digest}')


if __name__ == '__main__':
    main()

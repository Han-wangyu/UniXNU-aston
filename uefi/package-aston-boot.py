#!/usr/bin/env python3
"""Package Mu-Silicium SILICIUM_UEFI.fd + BootShim + cleaned aston.dtb into raw & AVB boot_a images."""
import argparse
import gzip
import hashlib
import shutil
import struct
import subprocess
import tempfile
import zlib
from pathlib import Path

EXPECTED_FD_BASE = 0xA7100000
EXPECTED_FD_SIZE = 0x300000
BOOT_A_PARTITION_BYTES = 201326592
PAGESIZE = 4096


def run_cmd(cmd: list[str], allow_fail: bool = False) -> subprocess.CompletedProcess[str]:
    res = subprocess.run(cmd, text=True, capture_output=True)
    if res.returncode != 0 and not allow_fail:
        raise RuntimeError(
            f"Command failed ({res.returncode}): {' '.join(cmd)}\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )
    return res


def prepare_clean_dtb(src_dtb: Path, dst_dtb: Path) -> bytes:
    shutil.copy2(src_dtb, dst_dtb)

    for prop in ("linux,initrd-start", "linux,initrd-end"):
        if run_cmd(["fdtget", str(dst_dtb), "/chosen", prop], allow_fail=True).returncode == 0:
            run_cmd(["fdtput", "-d", str(dst_dtb), "/chosen", prop])
        if run_cmd(["fdtget", str(dst_dtb), "/chosen", prop], allow_fail=True).returncode == 0:
            raise RuntimeError(f"Failed to remove /chosen/{prop} from {dst_dtb}")

    if run_cmd(["fdtget", str(dst_dtb), "/reserved-memory/splash_region", "reg"], allow_fail=True).returncode != 0:
        run_cmd(["fdtput", "-c", str(dst_dtb), "/reserved-memory/splash_region"])
    run_cmd(["fdtput", "-t", "s", str(dst_dtb), "/reserved-memory/splash_region", "label", "cont_splash_region"])
    run_cmd(
        ["fdtput", "-t", "x", str(dst_dtb), "/reserved-memory/splash_region", "reg", "0x0", "0xb8000000", "0x0", "0x2b00000"]
    )

    label = run_cmd(["fdtget", "-t", "s", str(dst_dtb), "/reserved-memory/splash_region", "label"]).stdout.strip()
    reg = [int(x, 16) for x in run_cmd(["fdtget", "-t", "x", str(dst_dtb), "/reserved-memory/splash_region", "reg"]).stdout.split()]
    if label != "cont_splash_region" or reg != [0x0, 0xB8000000, 0x0, 0x2B00000]:
        raise RuntimeError(f"Unexpected /reserved-memory/splash_region values: label={label!r}, reg={reg!r}")

    data = dst_dtb.read_bytes()
    if data[:4] != b"\xd0\x0d\xfe\xed" or struct.unpack_from(">I", data, 4)[0] != len(data):
        raise RuntimeError("Invalid FDT header/totalsize after fdtput")
    return data


def verify_boot_img(img_path: Path, combined_uefi: bytes, clean_dtb: bytes) -> None:
    data = img_path.read_bytes()
    if data[:8] != b"ANDROID!":
        raise RuntimeError(f"{img_path}: missing ANDROID! magic")
    kernel_size, ramdisk_size, _os_version, header_size, header_version = struct.unpack_from("<4I16xI", data, 8)
    if header_version != 4:
        raise RuntimeError(f"{img_path}: expected header_version 4, got {header_version}")
    if ramdisk_size != 0:
        raise RuntimeError(f"{img_path}: expected 0-byte ramdisk, got {ramdisk_size}")
    start = PAGESIZE * max(1, -(-header_size // PAGESIZE))
    kernel_blob = data[start : start + kernel_size]
    unpacker = zlib.decompressobj(16 + zlib.MAX_WBITS)
    unpacked = unpacker.decompress(kernel_blob)
    if unpacked != combined_uefi or not unpacker.eof or unpacker.unused_data != clean_dtb:
        raise RuntimeError(f"{img_path}: gzip(BootShim+UEFI_FD)+dtb round-trip mismatch")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mu", required=True, type=Path, help="Path to built Mu-Silicium checkout")
    parser.add_argument("--avbtool", required=True, type=Path, help="Path to AOSP avbtool.py")
    parser.add_argument("--key", required=True, type=Path, help="Path to testkey_rsa4096.pem")
    parser.add_argument("--name", required=True, help="Output variant name (e.g. aston-uefi-simplefb-probe)")
    parser.add_argument("--out-dir", required=True, type=Path, help="Output directory")
    parser.add_argument("--target", default="DEBUG", help="EDK II build target (default: DEBUG)")
    args = parser.parse_args()

    mu = args.mu.resolve()
    fd_path = mu / "Build" / "astonPkg" / f"{args.target}_CLANGPDB" / "FV" / "SILICIUM_UEFI.fd"
    shim_path = mu / "BootShim" / "BootShim.bin"
    dtb_path = mu / "Resources" / "DTBs" / "aston.dtb"
    mkbootimg_path = mu / "Resources" / "Scripts" / "mkbootimg.py"

    for p in (fd_path, shim_path, dtb_path, mkbootimg_path, args.avbtool, args.key):
        if not p.is_file():
            raise FileNotFoundError(f"Required input missing: {p}")

    fd_bytes = fd_path.read_bytes()
    if len(fd_bytes) != EXPECTED_FD_SIZE:
        raise RuntimeError(f"{fd_path}: expected {EXPECTED_FD_SIZE:#x} bytes, got {len(fd_bytes):#x}")

    shim_bytes = shim_path.read_bytes()
    if len(shim_bytes) < 0x40 or shim_bytes[56:60] != b"ARM\x64":
        raise RuntimeError(f"{shim_path}: missing ARM\\x64 header")
    fd_base, fd_size = struct.unpack_from("<QQ", shim_bytes, 8)
    if (fd_base, fd_size) != (EXPECTED_FD_BASE, EXPECTED_FD_SIZE):
        raise RuntimeError(
            f"{shim_path}: unexpected header (base={fd_base:#x}, size={fd_size:#x}), "
            f"expected ({EXPECTED_FD_BASE:#x}, {EXPECTED_FD_SIZE:#x})"
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    raw_img = args.out_dir / f"{args.name}.img"
    avb_img = args.out_dir / f"boot_a-{args.name}-avb.img"

    with tempfile.TemporaryDirectory(prefix="aston-bootimg-") as tmp:
        tmp_dir = Path(tmp)
        clean_dtb = prepare_clean_dtb(dtb_path, tmp_dir / "aston-clean.dtb")
        combined_uefi = shim_bytes + fd_bytes
        kernel_bytes = gzip.compress(combined_uefi, mtime=0) + clean_dtb
        kernel_path = tmp_dir / "Image.gz-dtb"
        kernel_path.write_bytes(kernel_bytes)
        empty_ramdisk = tmp_dir / "empty-ramdisk"
        empty_ramdisk.write_bytes(b"")

        run_cmd(
            [
                "python3",
                str(mkbootimg_path),
                "--kernel",
                str(kernel_path),
                "--ramdisk",
                str(empty_ramdisk),
                "--header_version",
                "4",
                "--os_version",
                "16.0.0",
                "--os_patch_level",
                "2026-06",
                "--pagesize",
                str(PAGESIZE),
                "-o",
                str(raw_img),
            ]
        )
        verify_boot_img(raw_img, combined_uefi, clean_dtb)

    shutil.copy2(raw_img, avb_img)
    run_cmd(
        [
            "python3",
            str(args.avbtool.resolve()),
            "add_hash_footer",
            "--image",
            str(avb_img),
            "--partition_name",
            "boot",
            "--partition_size",
            str(BOOT_A_PARTITION_BYTES),
            "--algorithm",
            "SHA256_RSA4096",
            "--key",
            str(args.key.resolve()),
        ]
    )

    if avb_img.stat().st_size != BOOT_A_PARTITION_BYTES:
        raise RuntimeError(f"{avb_img}: unexpected size {avb_img.stat().st_size} != {BOOT_A_PARTITION_BYTES}")
    with avb_img.open("rb") as f:
        f.seek(-64, 2)
        footer_magic = f.read(4)
    if footer_magic != b"AVBf":
        raise RuntimeError(f"{avb_img}: missing trailing AVBf footer magic (got {footer_magic!r})")

    digest_lines = [
        f"{sha256_file(raw_img)}  {raw_img.name}",
        f"{sha256_file(avb_img)}  {avb_img.name}",
    ]
    (args.out_dir / f"{args.name}.sha256").write_text("\n".join(digest_lines) + "\n", encoding="utf-8")
    print("\n".join(digest_lines))


if __name__ == "__main__":
    main()

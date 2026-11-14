# Open-Source OnePlus Ace 3 (`aston`) UniXNU UEFI Firmware

This directory replaces opaque prebuilt `Ace3-UniXNU` firmware images with a fully reproducible open-source [Project-Silicium/Mu-Silicium](https://github.com/Project-Silicium/Mu-Silicium) (`astonPkg`, pinned at `6b3d90b79e191e08171bfedd3980234a7e49d340`) build integrating:

- **`UniXNUPkg`**: `UniXNULib`, `UniXNUDxe`, and `UniXNU.efi` bootloader menu/loader
- **`DarwinBootPkg`**: `DarwinBoot.efi` XNU Mach-O + ADT + trustcache/dyld handoff driver (from `UniXNU/drivers/DarwinBoot`)
- **`AstonPkg`**:
  - `AstonConfigDxe`: reads the `SHNGROOT` v2 `Payload` FAT slice on the `unixnu` partition (`sda15`) into an in-memory UEFI BlockIo/SimpleFileSystem volume (`FallbackToml` when absent), and optionally runs non-destructive UEFI hardware probes (`PcdAstonProbeMode = 1`) writing `ASTON_UEFI_PROBE_V1` diagnostics to `sda15` LBA 0.
  - `AstonBootControlLib`: exact 78-entry Slot B partition GUID & attribute restoration table across UFS LUNs `sda`–`sdf` plus `BootLU` (`2`) switching.
  - `AstonSlotSwitch`, `AstonRebootRecovery`, `AstonRebootFastboot`: UEFI applications invoked from `UniXNU` menu entries to switch back to ColorOS (`Slot B`), reboot to recovery, or reboot to fastboot.

## Build Variants (`.github/workflows/uefi.yml`)

| Variant | `ASTON_CUSTOM_DISPLAY` | `ASTON_PROBE_MODE` | Purpose |
| --- | --- | --- | --- |
| `aston-uefi-simplefb-probe` | `0` | `1` | Probe GOP/memory/UniXNU protocols using bootloader SimpleFB; auto-reboot to recovery |
| `aston-uefi-displaydxe-probe` | `1` | `1` | Same probe with Qualcomm custom `DisplayDxe` initialized |
| `aston-uefi-simplefb-prod` | `0` | `0` | Production boot image (SimpleFB path) |
| `aston-uefi-displaydxe-prod` | `1` | `0` | Production boot image (`DisplayDxe` path) |

Each variant outputs:
- `<name>.img`: raw header v4 Android boot image for `fastboot boot` testing (`gzip(BootShim + SILICIUM_UEFI.fd) + clean aston.dtb`, 0-byte ramdisk, `os_version=16.0.0`, `os_patch_level=2026-06`).
- `boot_a-<name>-avb.img`: exact `201326592`-byte (`boot_a` / `sde13`) image with AVB v1 hash footer (`--partition_name boot --algorithm SHA256_RSA4096`) for permanent `boot_a` installation via `scripts/flash-aston-boot-a.py`.

## Integration Scripts

- `apply-aston-uefi.py`: populates `<mu>/Vendor/{UniXNUPkg,DarwinBootPkg,AstonPkg}` and idempotently patches `DeviceBuild.py`, `aston.dsc`, `aston.fdf`, and `Mu_Basecore/.../ArmGicV3Dxe.c` (preserving CRLF).
- `package-aston-boot.py`: strips `/chosen/linux,initrd-*` and ensures `/reserved-memory/splash_region` (`cont_splash_region`, `0xb8000000 + 0x2b00000`) in `aston.dtb`, compresses `BootShim.bin + SILICIUM_UEFI.fd`, packs `mkbootimg.py` v4 image, round-trip verifies gzip + DTB concatenation, and signs the 192 MiB `boot_a` image with `avbtool.py`.

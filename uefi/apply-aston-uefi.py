#!/usr/bin/env python3
"""Integrate UniXNU, DarwinBoot, and AstonPkg into a Project-Silicium/Mu-Silicium checkout."""
import argparse
import os
import shutil
from pathlib import Path

VENDOR_MODULES = [
    "DarwinBootPkg/Driver/DarwinBoot.inf",
    "UniXNUPkg/Driver/UniXNUDxe/UniXNUDxe.inf",
    "UniXNUPkg/Application/UniXNU/UniXNU.inf",
    "AstonPkg/Drivers/AstonConfigDxe/AstonConfigDxe.inf",
    "AstonPkg/Applications/AstonSlotSwitch/AstonSlotSwitch.inf",
    "AstonPkg/Applications/AstonRebootRecovery/AstonRebootRecovery.inf",
    "AstonPkg/Applications/AstonRebootFastboot/AstonRebootFastboot.inf",
]


def replace_once(text: str, old: str, new: str, sentinel: str, desc: str) -> str:
    if sentinel in text:
        return text
    if old not in text:
        raise RuntimeError(f"Cannot patch {desc}: expected anchor not found")
    return text.replace(old, new, 1)


def copy_pkg(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        src,
        dst,
        symlinks=True,
        ignore=shutil.ignore_patterns(".git", "Build", "build", ".cache", "__pycache__"),
    )


def patch_device_build(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        '        "Silicium-ACPI/Silicon/Qualcomm"\n    )',
        '        "Silicium-ACPI/Silicon/Qualcomm",\n        "Vendor"\n    )',
        '"Vendor"',
        f"{path} (PackagesPath)",
    )
    text = replace_once(
        text,
        '        self.env.SetValue ("BLD_*_FD_BLOCKS", self.env.GetValue("FD_BLOCKS"), "Default")\n\n        return 0',
        '        self.env.SetValue ("BLD_*_FD_BLOCKS", self.env.GetValue("FD_BLOCKS"), "Default")\n'
        '        self.env.SetValue ("BLD_*_USE_CUSTOM_DISPLAY_DRIVER", os.environ.get ("ASTON_CUSTOM_DISPLAY", "1"), "From env")\n'
        '        self.env.SetValue ("BLD_*_ASTON_PROBE_MODE", os.environ.get ("ASTON_PROBE_MODE", "0"), "From env")\n\n'
        '        return 0',
        "BLD_*_ASTON_PROBE_MODE",
        f"{path} (SetPlatformEnv)",
    )
    path.write_text(text, encoding="utf-8")


def patch_aston_dsc(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    text = replace_once(
        text,
        "  USE_CUSTOM_DISPLAY_DRIVER      = 1",
        "!ifndef USE_CUSTOM_DISPLAY_DRIVER\n"
        "  DEFINE USE_CUSTOM_DISPLAY_DRIVER = 1\n"
        "!endif\n"
        "!ifndef ASTON_PROBE_MODE\n"
        "  DEFINE ASTON_PROBE_MODE = 0\n"
        "!endif",
        "ASTON_PROBE_MODE",
        f"{path} (Defines)",
    )
    text = replace_once(
        text,
        "[PcdsFixedAtBuild]",
        "[PcdsFixedAtBuild]\n"
        "  gEfiMdePkgTokenSpaceGuid.PcdPlatformBootTimeOut|0\n"
        "  gAstonPkgTokenSpaceGuid.PcdAstonProbeMode|$(ASTON_PROBE_MODE)\n"
        "  gSiliciumPkgTokenSpaceGuid.PcdFrameBufferDelay|0\n"
        "!if $(TARGET) == DEBUG\n"
        "  gEfiMdePkgTokenSpaceGuid.PcdDebugPropertyMask|0x07\n"
        "!endif",
        "gAstonPkgTokenSpaceGuid.PcdAstonProbeMode",
        f"{path} (PcdsFixedAtBuild)",
    )
    text = replace_once(
        text,
        "  ConfigurationMapLib|astonPkg/Library/ConfigurationMapLib/ConfigurationMapLib.inf",
        "  ConfigurationMapLib|astonPkg/Library/ConfigurationMapLib/ConfigurationMapLib.inf\n"
        "  AstonBootControlLib|AstonPkg/Library/AstonBootControlLib/AstonBootControlLib.inf\n"
        "  UniXNULib|UniXNUPkg/Library/UniXNULib/UniXNULib.inf",
        "AstonBootControlLib|AstonPkg/Library/AstonBootControlLib/AstonBootControlLib.inf",
        f"{path} (LibraryClasses)",
    )
    components_block = "\n".join(f"  {mod}" for mod in VENDOR_MODULES)
    text = replace_once(
        text,
        "  aston/AcpiTables.inf",
        f"  aston/AcpiTables.inf\n\n  #\n  # UniXNU / DarwinBoot / Aston Control\n  #\n{components_block}",
        "AstonPkg/Drivers/AstonConfigDxe/AstonConfigDxe.inf",
        f"{path} (Components)",
    )
    path.write_text(text, encoding="utf-8")


def patch_aston_fdf(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    fdf_block = "\n".join(f"  INF {mod}" for mod in VENDOR_MODULES)
    text = replace_once(
        text,
        "  !include QcomPkg/Extra.fdf.inc",
        f"  # UniXNU / DarwinBoot / Aston Control\n{fdf_block}\n\n  !include QcomPkg/Extra.fdf.inc",
        "INF AstonPkg/Drivers/AstonConfigDxe/AstonConfigDxe.inf",
        f"{path} (FvMain)",
    )
    path.write_text(text, encoding="utf-8")


def patch_aston_memory_map(path: Path) -> None:
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")
    text = replace_once(
        text,
        newline.join(
            [
                '  {"HYP",               0x80000000, 0x00A00000, AddMem, SYS_MEM, SYS_MEM_CAP, Reserv, WRITE_BACK_XN},',
                '  {"CPUSYS VM",         0x80A00000, 0x00400000, AddMem, SYS_MEM, SYS_MEM_CAP, Reserv, WRITE_BACK_XN},',
            ]
        ),
        newline.join(
            [
                '  {"NOMAP",             0x80000000, 0x01200000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
                '  {"RSRV0",             0x81200000, 0x00800000, AddMem, SYS_MEM, SYS_MEM_CAP, Reserv, WRITE_BACK_XN},',
            ]
        ),
        '{"NOMAP",             0x80000000, 0x01200000,',
        f"{path} (HYP/CPUSYS VM -> NOMAP/RSRV0)",
    )
    text = replace_once(
        text,
        '  {"AOP",               0x81C00000, 0x000F4000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '  {"AOP",               0x81C00000, 0x000F5000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '{"AOP",               0x81C00000, 0x000F5000,',
        f"{path} (AOP + chipinfo_region)",
    )
    text = replace_once(
        text,
        newline.join(
            [
                '  {"UEFI FD Reserved",  0xA7400000, 0x00200000, AddMem, SYS_MEM, SYS_MEM_CAP, BsData, WRITE_BACK},',
                '  {"Info Blk",          0xA7601000, 0x00001000, AddMem, SYS_MEM, SYS_MEM_CAP, RtData, WRITE_BACK_XN},',
            ]
        ),
        newline.join(
            [
                '  {"UEFI FD Reserved",  0xA7400000, 0x00200000, AddMem, SYS_MEM, SYS_MEM_CAP, BsData, WRITE_BACK},',
                '  {"CPU Vectors",       0xA7600000, 0x00001000, AddMem, SYS_MEM, SYS_MEM_CAP, BsData, WRITE_BACK},',
                '  {"Info Blk",          0xA7601000, 0x00001000, AddMem, SYS_MEM, SYS_MEM_CAP, RtData, WRITE_BACK_XN},',
            ]
        ),
        '{"CPU Vectors",       0xA7600000, 0x00001000,',
        f"{path} (CPU Vectors)",
    )
    text = replace_once(
        text,
        '  {"HW Fence",          0xE6440000, 0x00279000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '  {"HW Fence",          0xE6440000, 0x002DD000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '{"HW Fence",          0xE6440000, 0x002DD000,',
        f"{path} (HW Fence)",
    )
    text = replace_once(
        text,
        '  {"LLCC LPI",          0xFF800000, 0x00600000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '  {"LLCC LPI",          0xFF800000, 0x00800000, AddMem, MEM_RES, UNCACHEABLE, Reserv, UNCACHED_UNBUFFERED_XN},',
        '{"LLCC LPI",          0xFF800000, 0x00800000,',
        f"{path} (LLCC LPI)",
    )
    path.write_bytes(text.encode("utf-8"))


def patch_gic_v3(path: Path) -> None:
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")
    old = newline.join(
        [
            "  } else {",
            '    DEBUG ((DEBUG_ERROR, "Spurious GIC interrupt: 0x%x\\n", (UINT32)GicInterrupt));',
            "    GicV3EndOfInterrupt (&gHardwareInterruptV3Protocol, GicInterrupt);",
            "  }",
        ]
    )
    new = newline.join(
        [
            "  } else {",
            '    DEBUG ((DEBUG_ERROR, "Spurious GIC interrupt: 0x%x\\n", (UINT32)GicInterrupt));',
            "    GicV3DisableInterruptSource (&gHardwareInterruptV3Protocol, GicInterrupt);",
            "    GicV3EndOfInterrupt (&gHardwareInterruptV3Protocol, GicInterrupt);",
            "  }",
        ]
    )
    updated = replace_once(
        text,
        old,
        new,
        "GicV3DisableInterruptSource (&gHardwareInterruptV3Protocol, GicInterrupt);",
        f"{path} (spurious EOI/disable)",
    )
    path.write_bytes(updated.encode("utf-8"))


def patch_psci_reset_lib(path: Path) -> None:
    raw = path.read_bytes()
    newline = "\r\n" if b"\r\n" in raw else "\n"
    text = raw.decode("utf-8")

    old_cold = newline.join(
        [
            "VOID",
            "EFIAPI",
            "ResetCold (",
            "  VOID",
            "  )",
            "{",
            "  ARM_MONITOR_ARGS  Args;",
            "",
            "  // Send a PSCI 0.2 SYSTEM_RESET command",
            "  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_RESET;",
            "",
            "  ArmMonitorCall (&Args);",
            "}",
        ]
    )
    new_cold = newline.join(
        [
            "STATIC",
            "VOID",
            "ZeroMonitorArgs (",
            "  OUT ARM_MONITOR_ARGS  *Args",
            "  )",
            "{",
            "  UINTN  Index;",
            "",
            "  for (Index = 0; Index < (sizeof (*Args) / sizeof (UINTN)); Index++) {",
            "    ((UINTN *)Args)[Index] = 0;",
            "  }",
            "}",
            "",
            "VOID",
            "EFIAPI",
            "ResetCold (",
            "  VOID",
            "  )",
            "{",
            "  ARM_MONITOR_ARGS  Args;",
            "",
            "  // Send a PSCI 0.2 SYSTEM_RESET command with zeroed register arguments.",
            "  ZeroMonitorArgs (&Args);",
            "  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_RESET;",
            "",
            "  ArmMonitorCall (&Args);",
            "}",
        ]
    )
    text = replace_once(
        text,
        old_cold,
        new_cold,
        f"ZeroMonitorArgs (&Args);{newline}  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_RESET;",
        f"{path} (ResetCold)",
    )

    old_warm = newline.join(
        [
            "VOID",
            "EFIAPI",
            "ResetWarm (",
            "  VOID",
            "  )",
            "{",
            "  ARM_MONITOR_ARGS  Args;",
            "",
            "  Args.Arg0 = ARM_SMC_ID_PSCI_FEATURES;",
            "  Args.Arg1 = ARM_SMC_ID_PSCI_SYSTEM_RESET2_AARCH64;",
            "",
            "  // Is SYSTEM_RESET2 supported?",
            "  ArmMonitorCall (&Args);",
            "  if (Args.Arg0 == ARM_SMC_PSCI_RET_SUCCESS) {",
            "    // Send PSCI SYSTEM_RESET2 command",
            "    Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_RESET2_AARCH64;",
            "    Args.Arg1 = 0; // Reset type",
            "    // cookie does not matter with reset type 0",
            "",
            "    ArmMonitorCall (&Args);",
            "  } else {",
            "    // Map a warm reset into a cold reset",
            "    DEBUG ((",
            "      DEBUG_INFO,",
            '      "Warm reboot not supported by platform, issuing cold reboot\\n"',
            "      ));",
            "    ResetCold ();",
            "  }",
            "}",
        ]
    )
    new_warm = newline.join(
        [
            "VOID",
            "EFIAPI",
            "ResetWarm (",
            "  VOID",
            "  )",
            "{",
            "  // Qualcomm SM8550 TZ rejects PSCI SYSTEM_RESET2(0); always issue cold reset.",
            "  ResetCold ();",
            "}",
        ]
    )
    text = replace_once(
        text,
        old_warm,
        new_warm,
        "Qualcomm SM8550 TZ rejects PSCI SYSTEM_RESET2",
        f"{path} (ResetWarm)",
    )

    old_shutdown = newline.join(
        [
            "VOID",
            "EFIAPI",
            "ResetShutdown (",
            "  VOID",
            "  )",
            "{",
            "  ARM_MONITOR_ARGS  Args;",
            "",
            "  // Send a PSCI 0.2 SYSTEM_RESET command",
            "  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_OFF;",
            "",
            "  ArmMonitorCall (&Args);",
            "}",
        ]
    )
    new_shutdown = newline.join(
        [
            "VOID",
            "EFIAPI",
            "ResetShutdown (",
            "  VOID",
            "  )",
            "{",
            "  ARM_MONITOR_ARGS  Args;",
            "",
            "  // Send a PSCI 0.2 SYSTEM_OFF command with zeroed register arguments.",
            "  ZeroMonitorArgs (&Args);",
            "  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_OFF;",
            "",
            "  ArmMonitorCall (&Args);",
            "}",
        ]
    )
    text = replace_once(
        text,
        old_shutdown,
        new_shutdown,
        f"ZeroMonitorArgs (&Args);{newline}  Args.Arg0 = ARM_SMC_ID_PSCI_SYSTEM_OFF;",
        f"{path} (ResetShutdown)",
    )
    path.write_bytes(text.encode("utf-8"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mu", required=True, type=Path, help="Path to Mu-Silicium checkout")
    parser.add_argument("--unixnu", required=True, type=Path, help="Path to UniXNU checkout")
    parser.add_argument("--aston", required=True, type=Path, help="Path to UniXNU-aston checkout")
    parser.add_argument(
        "--darwinboot",
        type=Path,
        default=None,
        help="Optional path to DarwinBoot repo or DarwinBootPkg (defaults to <unixnu>/drivers/DarwinBoot)",
    )
    args = parser.parse_args()

    mu = args.mu.resolve()
    unixnu = args.unixnu.resolve()
    aston = args.aston.resolve()

    unixnu_pkg = unixnu / "package" / "UniXNUPkg"
    if not (unixnu_pkg / "UniXNUPkg.dec").is_file():
        raise FileNotFoundError(f"UniXNUPkg missing at {unixnu_pkg}")

    db_base = args.darwinboot.resolve() if args.darwinboot else (unixnu / "drivers" / "DarwinBoot")
    darwinboot_pkg = db_base if (db_base / "Driver" / "DarwinBoot.inf").is_file() else (db_base / "DarwinBootPkg")
    if not (darwinboot_pkg / "Driver" / "DarwinBoot.inf").is_file():
        raise FileNotFoundError(
            f"DarwinBootPkg missing at {darwinboot_pkg} "
            "(ensure `git submodule update --init --depth 1 drivers/DarwinBoot` ran in UniXNU)"
        )

    aston_pkg = aston / "uefi" / "AstonPkg"
    if not (aston_pkg / "AstonPkg.dec").is_file():
        raise FileNotFoundError(f"AstonPkg missing at {aston_pkg}")

    vendor = mu / "Vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    copy_pkg(unixnu_pkg, vendor / "UniXNUPkg")
    copy_pkg(darwinboot_pkg, vendor / "DarwinBootPkg")
    copy_pkg(aston_pkg, vendor / "AstonPkg")

    patch_device_build(mu / "Platforms" / "OnePlus" / "astonPkg" / "DeviceBuild.py")
    patch_aston_dsc(mu / "Platforms" / "OnePlus" / "astonPkg" / "aston.dsc")
    patch_aston_fdf(mu / "Platforms" / "OnePlus" / "astonPkg" / "aston.fdf")
    patch_aston_memory_map(mu / "Platforms" / "OnePlus" / "astonPkg" / "Library" / "MemoryMapLib" / "MemoryMapLib.c")
    patch_gic_v3(mu / "Mu_Basecore" / "ArmPkg" / "Drivers" / "ArmGicDxe" / "GicV3" / "ArmGicV3Dxe.c")
    patch_psci_reset_lib(mu / "Mu_Basecore" / "ArmPkg" / "Library" / "ArmPsciResetSystemLib" / "ArmPsciResetSystemLib.c")

    print(
        f"Applied Aston UniXNU UEFI integration into {mu} "
        f"(ASTON_CUSTOM_DISPLAY={os.environ.get('ASTON_CUSTOM_DISPLAY', 'unset')}, "
        f"ASTON_PROBE_MODE={os.environ.get('ASTON_PROBE_MODE', 'unset')})"
    )


if __name__ == "__main__":
    main()

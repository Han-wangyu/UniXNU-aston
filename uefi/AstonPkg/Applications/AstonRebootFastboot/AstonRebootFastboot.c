/** @file
  AstonRebootFastboot: reboot straight back to the bootloader (fastboot).

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>
#include <Library/AstonBootControlLib.h>
#include <Library/UefiApplicationEntryPoint.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>

EFI_STATUS
EFIAPI
UefiMain (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  Print (L"AstonRebootFastboot: rebooting to bootloader...\n");
  gBS->Stall (300000);
  AstonRebootWithReason (ASTON_REBOOT_BOOTLOADER);
  return EFI_SUCCESS;
}

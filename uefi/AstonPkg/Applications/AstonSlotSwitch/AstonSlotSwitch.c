/** @file
  AstonSlotSwitch: switch active slot to Slot B (ColorOS) and cold/warm reboot.

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
  EFI_STATUS  Status;
  UINT32      UpdatedLuns;
  UINT32      UpdatedEntries;

  UpdatedLuns    = 0;
  UpdatedEntries = 0;
  Status         = AstonSwitchToSlotB (&UpdatedLuns, &UpdatedEntries);
  Print (
    L"AstonSlotSwitch: slot B switch -> %r (LUNs=%u, entries=%u)\n",
    Status,
    UpdatedLuns,
    UpdatedEntries
    );
  gBS->Stall (500000);
  AstonRebootWithReason (ASTON_REBOOT_NORMAL);
  return Status;
}

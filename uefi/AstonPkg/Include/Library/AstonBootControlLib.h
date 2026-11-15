/** @file
  Public API for OnePlus Ace 3 (aston) GPT partition lookup, A/B slot switching,
  reboot reason programming, and SHA-256 verification.

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#ifndef ASTON_BOOT_CONTROL_LIB_H_
#define ASTON_BOOT_CONTROL_LIB_H_

#include <Uefi.h>
#include <Protocol/BlockIo.h>

#define ASTON_REBOOT_NORMAL      0
#define ASTON_REBOOT_RECOVERY    1
#define ASTON_REBOOT_BOOTLOADER  2

typedef struct {
  EFI_BLOCK_IO_PROTOCOL  *BlockIo;
  UINT32                 MediaId;
  UINT64                 StartingLba;
  UINT64                 EndingLba;
  UINT32                 BlockSize;
} ASTON_GPT_PARTITION;

VOID
EFIAPI
AstonSha256 (
  IN  CONST VOID  *Data,
  IN  UINTN       Length,
  OUT UINT8       Digest[32]
  );

VOID
EFIAPI
AstonConnectAllControllers (
  VOID
  );

EFI_STATUS
EFIAPI
AstonFindGptPartition (
  IN  CONST CHAR16         *PartitionName,
  OUT ASTON_GPT_PARTITION  *Partition
  );

EFI_STATUS
EFIAPI
AstonGetBootLu (
  OUT UINT32  *BootLu
  );

EFI_STATUS
EFIAPI
AstonSwitchToSlotB (
  OUT UINT32  *UpdatedLuns OPTIONAL,
  OUT UINT32  *UpdatedEntries OPTIONAL
  );

VOID
EFIAPI
AstonRebootWithReason (
  IN UINT8  Reason
  );

#endif

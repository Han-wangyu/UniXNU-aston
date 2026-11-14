/** @file
  AstonConfigDxe: reads SHNGROOT v2 "Payload" from GPT partition "unixnu" into
  RAM, verifies both header and object SHA-256 digests, and exposes a read-only
  512-byte EFI_BLOCK_IO_PROTOCOL volume for UniXNU (or a safe fallback
  SimpleFileSystem \EFI\UniXNU\unixnu.toml when no payload is present).

  When PcdAstonProbeMode != 0, writes ASTON_UEFI_PROBE_V1 hardware
  telemetry (GOP, high memory, UniXNULoader, BootLU, unixnu LBA geometry) to
  LBA 0 of "unixnu" on ReadyToBoot and reboots cleanly back to fastboot.

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <PiDxe.h>
#include <Guid/FileInfo.h>
#include <Library/AstonBootControlLib.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/DevicePathLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/PcdLib.h>
#include <Library/PrintLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiLib.h>
#include <Protocol/BlockIo.h>
#include <Protocol/DevicePath.h>
#include <Protocol/GraphicsOutput.h>
#include <Protocol/SimpleFileSystem.h>

#define ASTON_SHNGROOT_MAGIC          "SHNGROOT"
#define ASTON_SHNGROOT_VERSION        2U
#define ASTON_SHNGROOT_MAX_ENTRIES    31U
#define ASTON_MAX_PAYLOAD_BYTES       (256ULL * 1024ULL * 1024ULL)
#define ASTON_READ_CHUNK_BYTES        (4U * 1024U * 1024U)

STATIC CONST EFI_GUID  mUniXnuLoaderProtocolGuid = {
  0x0f0a492e, 0x5a6a, 0x49f6, { 0xa1, 0xbf, 0x74, 0x3c, 0x87, 0xab, 0x1c, 0xfe }
};

#pragma pack(1)
typedef struct {
  CHAR8   Magic[8];
  UINT32  Version;
  UINT32  Count;
  UINT64  TotalBytes;
} ASTON_SHNGROOT_HEADER;

typedef struct {
  CHAR8   Name[32];
  UINT64  Offset;
  UINT64  Size;
  UINT32  BlockSize;
  UINT32  Flags;
  UINT8   Sha256[32];
  UINT8   Reserved[40];
} ASTON_SHNGROOT_ENTRY;

typedef struct {
  VENDOR_DEVICE_PATH        Vendor;
  EFI_DEVICE_PATH_PROTOCOL  End;
} ASTON_VENDOR_DEVICE_PATH;
#pragma pack()

STATIC ASTON_VENDOR_DEVICE_PATH  mPayloadDevicePath = {
  {
    { HARDWARE_DEVICE_PATH, HW_VENDOR_DP, { sizeof (VENDOR_DEVICE_PATH), 0 } },
    { 0x5f4e6d21, 0x8b14, 0x43c0, { 0x99, 0xa2, 0x18, 0x7c, 0x34, 0x90, 0x01, 0x11 } }
  },
  { END_DEVICE_PATH_TYPE, END_ENTIRE_DEVICE_PATH_SUBTYPE, { sizeof (EFI_DEVICE_PATH_PROTOCOL), 0 } }
};

STATIC ASTON_VENDOR_DEVICE_PATH  mFallbackFsDevicePath = {
  {
    { HARDWARE_DEVICE_PATH, HW_VENDOR_DP, { sizeof (VENDOR_DEVICE_PATH), 0 } },
    { 0x5f4e6d22, 0x8b14, 0x43c0, { 0x99, 0xa2, 0x18, 0x7c, 0x34, 0x90, 0x02, 0x22 } }
  },
  { END_DEVICE_PATH_TYPE, END_ENTIRE_DEVICE_PATH_SUBTYPE, { sizeof (EFI_DEVICE_PATH_PROTOCOL), 0 } }
};

//
// In-memory read-only RAM BlockIo for "Payload" FAT image
//
typedef struct {
  EFI_BLOCK_IO_PROTOCOL  BlockIo;
  EFI_BLOCK_IO_MEDIA     Media;
  UINT8                  *Buffer;
  UINTN                  Size;
} ASTON_RAM_BLOCK_DEV;

STATIC ASTON_RAM_BLOCK_DEV  *mRamDisk  = NULL;
STATIC EFI_HANDLE           mRamHandle = NULL;
STATIC BOOLEAN              mPublished = FALSE;

STATIC
EFI_STATUS
EFIAPI
RamBlockReset (
  IN EFI_BLOCK_IO_PROTOCOL  *This,
  IN BOOLEAN                ExtendedVerification
  )
{
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockRead (
  IN  EFI_BLOCK_IO_PROTOCOL  *This,
  IN  UINT32                 MediaId,
  IN  EFI_LBA                Lba,
  IN  UINTN                  BufferSize,
  OUT VOID                   *Buffer
  )
{
  UINTN  Offset;

  if ((This == NULL) || (This->Media == NULL) || (Buffer == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  if (MediaId != This->Media->MediaId) {
    return EFI_MEDIA_CHANGED;
  }

  if (BufferSize == 0) {
    return EFI_SUCCESS;
  }

  if ((BufferSize % This->Media->BlockSize) != 0) {
    return EFI_BAD_BUFFER_SIZE;
  }

  Offset = (UINTN)Lba * (UINTN)This->Media->BlockSize;
  if ((Lba > This->Media->LastBlock) || (Offset + BufferSize > mRamDisk->Size)) {
    return EFI_INVALID_PARAMETER;
  }

  CopyMem (Buffer, mRamDisk->Buffer + Offset, BufferSize);
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockWrite (
  IN EFI_BLOCK_IO_PROTOCOL  *This,
  IN UINT32                 MediaId,
  IN EFI_LBA                Lba,
  IN UINTN                  BufferSize,
  IN VOID                   *Buffer
  )
{
  return EFI_WRITE_PROTECTED;
}

STATIC
EFI_STATUS
EFIAPI
RamBlockFlush (
  IN EFI_BLOCK_IO_PROTOCOL  *This
  )
{
  return EFI_SUCCESS;
}

//
// Read-only in-memory fallback SimpleFileSystem serving \EFI\UniXNU\unixnu.toml
//
STATIC CONST CHAR8  mFallbackToml[] =
  "version = 1\n\n"
  "[boot]\n"
  "timeout = 5\n"
  "default = \"slot-b\"\n"
  "rotate = 0\n\n"
  "[[entry]]\n"
  "id = \"slot-b\"\n"
  "title = \"Switch to slot B (ColorOS)\"\n"
  "type = \"efi\"\n"
  "path = \"fv:7C4F0B9E-3D2A-4B61-9E85-2F6A1C0D7B43\"\n\n"
  "[[entry]]\n"
  "id = \"recovery\"\n"
  "title = \"Reboot to TWRP Recovery\"\n"
  "type = \"efi\"\n"
  "path = \"fv:8D5E1C2A-4F3B-4E72-A9C6-3B7F2D1E8A54\"\n\n"
  "[[entry]]\n"
  "id = \"fastboot\"\n"
  "title = \"Reboot to Bootloader (Fastboot)\"\n"
  "type = \"efi\"\n"
  "path = \"fv:9E6F2D3B-5A4C-4F83-B8D7-4C8E3F2A9B65\"\n\n"
  "[[entry]]\n"
  "id = \"shutdown\"\n"
  "title = \"Power off\"\n"
  "type = \"action\"\n"
  "action = \"shutdown\"\n";

typedef struct {
  EFI_FILE_PROTOCOL  Protocol;
  BOOLEAN            IsDir;
  UINTN              Pos;
} ASTON_MEM_FILE;

STATIC
EFI_STATUS
EFIAPI
MemFileOpen (
  IN  EFI_FILE_PROTOCOL  *This,
  OUT EFI_FILE_PROTOCOL  **NewHandle,
  IN  CHAR16             *FileName,
  IN  UINT64             OpenMode,
  IN  UINT64             Attributes
  )
{
  ASTON_MEM_FILE  *Child;
  BOOLEAN         IsDir;

  if ((This == NULL) || (NewHandle == NULL) || (FileName == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  if ((OpenMode & EFI_FILE_MODE_WRITE) != 0) {
    return EFI_WRITE_PROTECTED;
  }

  if ((StrCmp (FileName, L"\\EFI\\UniXNU\\unixnu.toml") == 0) ||
      (StrCmp (FileName, L"EFI\\UniXNU\\unixnu.toml") == 0) ||
      (StrCmp (FileName, L"unixnu.toml") == 0))
  {
    IsDir = FALSE;
  } else if ((StrCmp (FileName, L"\\EFI") == 0) ||
             (StrCmp (FileName, L"EFI") == 0) ||
             (StrCmp (FileName, L"\\EFI\\UniXNU") == 0) ||
             (StrCmp (FileName, L"EFI\\UniXNU") == 0) ||
             (StrCmp (FileName, L"\\") == 0) ||
             (StrCmp (FileName, L".") == 0))
  {
    IsDir = TRUE;
  } else {
    return EFI_NOT_FOUND;
  }

  Child = AllocateCopyPool (sizeof (ASTON_MEM_FILE), This);
  if (Child == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  Child->IsDir = IsDir;
  Child->Pos   = 0;
  *NewHandle   = &Child->Protocol;
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileClose (
  IN EFI_FILE_PROTOCOL  *This
  )
{
  FreePool (This);
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileDelete (
  IN EFI_FILE_PROTOCOL  *This
  )
{
  return EFI_WARN_DELETE_FAILURE;
}

STATIC
EFI_STATUS
EFIAPI
MemFileRead (
  IN     EFI_FILE_PROTOCOL  *This,
  IN OUT UINTN              *BufferSize,
  OUT    VOID               *Buffer
  )
{
  ASTON_MEM_FILE  *File;
  UINTN           Total;
  UINTN           Avail;
  UINTN           ToCopy;

  if ((This == NULL) || (BufferSize == NULL) || (Buffer == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  File = (ASTON_MEM_FILE *)This;
  if (File->IsDir) {
    *BufferSize = 0;
    return EFI_SUCCESS;
  }

  Total = sizeof (mFallbackToml) - 1;
  if (File->Pos >= Total) {
    *BufferSize = 0;
    return EFI_SUCCESS;
  }

  Avail  = Total - File->Pos;
  ToCopy = (*BufferSize < Avail) ? *BufferSize : Avail;
  CopyMem (Buffer, mFallbackToml + File->Pos, ToCopy);
  File->Pos  += ToCopy;
  *BufferSize = ToCopy;
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileWrite (
  IN     EFI_FILE_PROTOCOL  *This,
  IN OUT UINTN              *BufferSize,
  IN     VOID               *Buffer
  )
{
  return EFI_WRITE_PROTECTED;
}

STATIC
EFI_STATUS
EFIAPI
MemFileGetPosition (
  IN  EFI_FILE_PROTOCOL  *This,
  OUT UINT64             *Position
  )
{
  ASTON_MEM_FILE  *File;

  if ((This == NULL) || (Position == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  File      = (ASTON_MEM_FILE *)This;
  *Position = (UINT64)File->Pos;
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileSetPosition (
  IN EFI_FILE_PROTOCOL  *This,
  IN UINT64             Position
  )
{
  ASTON_MEM_FILE  *File;
  UINTN           Total;

  if (This == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  File  = (ASTON_MEM_FILE *)This;
  Total = File->IsDir ? 0 : (sizeof (mFallbackToml) - 1);
  if (Position == 0xFFFFFFFFFFFFFFFFULL) {
    File->Pos = Total;
  } else if (Position <= Total) {
    File->Pos = (UINTN)Position;
  } else {
    return EFI_UNSUPPORTED;
  }

  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileGetInfo (
  IN     EFI_FILE_PROTOCOL  *This,
  IN     EFI_GUID           *InformationType,
  IN OUT UINTN              *BufferSize,
  OUT    VOID               *Buffer
  )
{
  ASTON_MEM_FILE  *File;
  CONST CHAR16    *Name;
  UINTN           NeedSize;
  EFI_FILE_INFO   *Info;

  if ((This == NULL) || (InformationType == NULL) || (BufferSize == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  if (!CompareGuid (InformationType, &gEfiFileInfoGuid)) {
    return EFI_UNSUPPORTED;
  }

  File     = (ASTON_MEM_FILE *)This;
  Name     = File->IsDir ? L"\\" : L"unixnu.toml";
  NeedSize = OFFSET_OF (EFI_FILE_INFO, FileName) + StrSize (Name);
  if (*BufferSize < NeedSize) {
    *BufferSize = NeedSize;
    return EFI_BUFFER_TOO_SMALL;
  }

  if (Buffer == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  ZeroMem (Buffer, NeedSize);
  Info               = (EFI_FILE_INFO *)Buffer;
  Info->Size         = NeedSize;
  Info->FileSize     = File->IsDir ? 0 : (sizeof (mFallbackToml) - 1);
  Info->PhysicalSize = Info->FileSize;
  Info->Attribute    = EFI_FILE_READ_ONLY | (File->IsDir ? EFI_FILE_DIRECTORY : 0);
  CopyMem (Info->FileName, Name, StrSize (Name));
  *BufferSize = NeedSize;
  return EFI_SUCCESS;
}

STATIC
EFI_STATUS
EFIAPI
MemFileSetInfo (
  IN EFI_FILE_PROTOCOL  *This,
  IN EFI_GUID           *InformationType,
  IN UINTN              BufferSize,
  IN VOID               *Buffer
  )
{
  return EFI_WRITE_PROTECTED;
}

STATIC
EFI_STATUS
EFIAPI
MemFileFlush (
  IN EFI_FILE_PROTOCOL  *This
  )
{
  return EFI_SUCCESS;
}

STATIC CONST EFI_FILE_PROTOCOL  mMemFileTemplate = {
  EFI_FILE_PROTOCOL_REVISION,
  MemFileOpen,
  MemFileClose,
  MemFileDelete,
  MemFileRead,
  MemFileWrite,
  MemFileGetPosition,
  MemFileSetPosition,
  MemFileGetInfo,
  MemFileSetInfo,
  MemFileFlush,
  NULL,
  NULL,
  NULL,
  NULL
};

STATIC
EFI_STATUS
EFIAPI
FallbackOpenVolume (
  IN  EFI_SIMPLE_FILE_SYSTEM_PROTOCOL  *This,
  OUT EFI_FILE_PROTOCOL                **Root
  )
{
  ASTON_MEM_FILE  *File;

  if (Root == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  File = AllocateZeroPool (sizeof (ASTON_MEM_FILE));
  if (File == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  CopyMem (&File->Protocol, &mMemFileTemplate, sizeof (EFI_FILE_PROTOCOL));
  File->IsDir = TRUE;
  File->Pos   = 0;
  *Root       = &File->Protocol;
  return EFI_SUCCESS;
}

STATIC EFI_SIMPLE_FILE_SYSTEM_PROTOCOL  mFallbackFs = {
  EFI_SIMPLE_FILE_SYSTEM_PROTOCOL_REVISION,
  FallbackOpenVolume
};

STATIC
VOID
InstallFallbackFs (
  VOID
  )
{
  STATIC BOOLEAN  mFallbackInstalled = FALSE;
  EFI_HANDLE      Handle;
  EFI_STATUS      Status;

  if (mFallbackInstalled) {
    return;
  }

  Handle = NULL;
  Status = gBS->InstallMultipleProtocolInterfaces (
                  &Handle,
                  &gEfiDevicePathProtocolGuid,
                  &mFallbackFsDevicePath,
                  &gEfiSimpleFileSystemProtocolGuid,
                  &mFallbackFs,
                  NULL
                  );
  if (!EFI_ERROR (Status)) {
    mFallbackInstalled = TRUE;
  }

  DEBUG ((DEBUG_INFO, "AstonConfigDxe: fallback SimpleFileSystem -> %r\n", Status));
}

STATIC
EFI_STATUS
TryPublishUnixnuPayload (
  VOID
  )
{
  EFI_STATUS             Status;
  ASTON_GPT_PARTITION    Unixnu;
  UINT8                  Sector[4096];
  ASTON_SHNGROOT_HEADER  *Hdr;
  UINT8                  Digest[32];
  ASTON_SHNGROOT_ENTRY   *Entry;
  UINT32                 Index;
  UINT64                 PartBytes;
  UINT64                 PayloadSize;
  UINT64                 PayloadOffset;
  UINT8                  *Buffer;
  UINTN                  AllocBytes;
  UINTN                  Done;
  UINTN                  Chunk;
  EFI_HANDLE             RamHandle;

  if (mPublished) {
    return EFI_SUCCESS;
  }

  Status = AstonFindGptPartition (L"unixnu", &Unixnu);
  if (EFI_ERROR (Status)) {
    return Status;
  }

  Status = Unixnu.BlockIo->ReadBlocks (Unixnu.BlockIo, Unixnu.MediaId, Unixnu.StartingLba, sizeof (Sector), Sector);
  if (EFI_ERROR (Status)) {
    return Status;
  }

  Hdr = (ASTON_SHNGROOT_HEADER *)Sector;
  if ((CompareMem (Hdr->Magic, ASTON_SHNGROOT_MAGIC, 8) != 0) ||
      (Hdr->Version != ASTON_SHNGROOT_VERSION) ||
      (Hdr->Count == 0) ||
      (Hdr->Count > ASTON_SHNGROOT_MAX_ENTRIES))
  {
    return EFI_NOT_FOUND;
  }

  AstonSha256 (Sector, 4064, Digest);
  if (CompareMem (Digest, Sector + 4064, 32) != 0) {
    DEBUG ((DEBUG_ERROR, "AstonConfigDxe: SHNGROOT header SHA-256 mismatch\n"));
    return EFI_CRC_ERROR;
  }

  PartBytes = (Unixnu.EndingLba - Unixnu.StartingLba + 1ULL) * (UINT64)Unixnu.BlockSize;
  Entry     = NULL;
  for (Index = 0; Index < Hdr->Count; Index++) {
    ASTON_SHNGROOT_ENTRY  *Cand = (ASTON_SHNGROOT_ENTRY *)(Sector + 24 + (UINTN)Index * sizeof (ASTON_SHNGROOT_ENTRY));
    if (AsciiStrCmp (Cand->Name, "Payload") == 0) {
      Entry = Cand;
      break;
    }
  }

  if (Entry == NULL) {
    return EFI_NOT_FOUND;
  }

  PayloadOffset = Entry->Offset;
  PayloadSize   = Entry->Size;
  if ((PayloadSize == 0) ||
      (PayloadSize > ASTON_MAX_PAYLOAD_BYTES) ||
      ((PayloadSize % 512ULL) != 0) ||
      ((PayloadOffset % Unixnu.BlockSize) != 0) ||
      (PayloadOffset + PayloadSize > PartBytes))
  {
    return EFI_BAD_BUFFER_SIZE;
  }

  AllocBytes = (UINTN)ALIGN_VALUE (PayloadSize, Unixnu.BlockSize);
  Buffer     = AllocatePages (EFI_SIZE_TO_PAGES (AllocBytes));
  if (Buffer == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  Done = 0;
  while (Done < AllocBytes) {
    Chunk  = MIN ((UINTN)ASTON_READ_CHUNK_BYTES, AllocBytes - Done);
    Status = Unixnu.BlockIo->ReadBlocks (
                               Unixnu.BlockIo,
                               Unixnu.MediaId,
                               Unixnu.StartingLba + (EFI_LBA)((PayloadOffset + Done) / Unixnu.BlockSize),
                               Chunk,
                               Buffer + Done
                               );
    if (EFI_ERROR (Status)) {
      FreePages (Buffer, EFI_SIZE_TO_PAGES (AllocBytes));
      return Status;
    }

    Done += Chunk;
  }

  AstonSha256 (Buffer, (UINTN)PayloadSize, Digest);
  if (CompareMem (Digest, Entry->Sha256, 32) != 0) {
    DEBUG ((DEBUG_ERROR, "AstonConfigDxe: Payload SHA-256 mismatch\n"));
    FreePages (Buffer, EFI_SIZE_TO_PAGES (AllocBytes));
    return EFI_CRC_ERROR;
  }

  mRamDisk = AllocateZeroPool (sizeof (ASTON_RAM_BLOCK_DEV));
  if (mRamDisk == NULL) {
    FreePages (Buffer, EFI_SIZE_TO_PAGES (AllocBytes));
    return EFI_OUT_OF_RESOURCES;
  }

  mRamDisk->Buffer                     = Buffer;
  mRamDisk->Size                       = (UINTN)PayloadSize;
  mRamDisk->Media.MediaId              = 1;
  mRamDisk->Media.RemovableMedia       = FALSE;
  mRamDisk->Media.MediaPresent         = TRUE;
  mRamDisk->Media.LogicalPartition     = FALSE;
  mRamDisk->Media.ReadOnly             = TRUE;
  mRamDisk->Media.WriteCaching         = FALSE;
  mRamDisk->Media.BlockSize            = 512;
  mRamDisk->Media.IoAlign              = 1;
  mRamDisk->Media.LastBlock            = (PayloadSize / 512ULL) - 1ULL;
  mRamDisk->BlockIo.Revision           = EFI_BLOCK_IO_PROTOCOL_REVISION3;
  mRamDisk->BlockIo.Media              = &mRamDisk->Media;
  mRamDisk->BlockIo.Reset              = RamBlockReset;
  mRamDisk->BlockIo.ReadBlocks         = RamBlockRead;
  mRamDisk->BlockIo.WriteBlocks        = RamBlockWrite;
  mRamDisk->BlockIo.FlushBlocks        = RamBlockFlush;

  RamHandle = NULL;
  Status    = gBS->InstallMultipleProtocolInterfaces (
                     &RamHandle,
                     &gEfiDevicePathProtocolGuid,
                     &mPayloadDevicePath,
                     &gEfiBlockIoProtocolGuid,
                     &mRamDisk->BlockIo,
                     NULL
                     );
  if (EFI_ERROR (Status)) {
    FreePool (mRamDisk);
    mRamDisk = NULL;
    FreePages (Buffer, EFI_SIZE_TO_PAGES (AllocBytes));
    return Status;
  }

  mRamHandle = RamHandle;
  gBS->ConnectController (RamHandle, NULL, NULL, TRUE);
  mPublished = TRUE;
  DEBUG ((DEBUG_INFO, "AstonConfigDxe: published SHNGROOT Payload (%lu bytes)\n", PayloadSize));
  return EFI_SUCCESS;
}

STATIC
VOID
RunUefiHardwareProbeAndReboot (
  VOID
  )
{
  EFI_STATUS                    Status;
  EFI_GRAPHICS_OUTPUT_PROTOCOL  *Gop;
  UINTN                         GopW;
  UINTN                         GopH;
  UINT64                        FbBase;
  UINTN                         FbSize;
  UINTN                         MapSize;
  EFI_MEMORY_DESCRIPTOR         *Map;
  UINTN                         MapKey;
  UINTN                         DescSize;
  UINT32                        DescVer;
  UINT64                        TotalConvBytes;
  UINT64                        HighConvBytes;
  UINTN                         Offset;
  VOID                          *Loader;
  BOOLEAN                       HasLoader;
  UINT32                        BootLu;
  EFI_STATUS                    BootLuStatus;
  ASTON_GPT_PARTITION           Unixnu;
  EFI_STATUS                    UnixnuStatus;
  UINT64                        UnixnuBlocks;
  CHAR8                         Report[4096];

  GopW   = 0;
  GopH   = 0;
  FbBase = 0;
  FbSize = 0;
  if (!EFI_ERROR (gBS->LocateProtocol (&gEfiGraphicsOutputProtocolGuid, NULL, (VOID **)&Gop)) &&
      (Gop != NULL) && (Gop->Mode != NULL) && (Gop->Mode->Info != NULL))
  {
    GopW   = Gop->Mode->Info->HorizontalResolution;
    GopH   = Gop->Mode->Info->VerticalResolution;
    FbBase = (UINT64)Gop->Mode->FrameBufferBase;
    FbSize = Gop->Mode->FrameBufferSize;
  }

  TotalConvBytes = 0;
  HighConvBytes  = 0;
  MapSize        = 0;
  Map            = NULL;
  Status         = gBS->GetMemoryMap (&MapSize, Map, &MapKey, &DescSize, &DescVer);
  if (Status == EFI_BUFFER_TOO_SMALL) {
    MapSize += 16 * DescSize;
    Map      = AllocatePool (MapSize);
    if ((Map != NULL) && !EFI_ERROR (gBS->GetMemoryMap (&MapSize, Map, &MapKey, &DescSize, &DescVer))) {
      for (Offset = 0; Offset + DescSize <= MapSize; Offset += DescSize) {
        EFI_MEMORY_DESCRIPTOR  *Desc = (EFI_MEMORY_DESCRIPTOR *)((UINT8 *)Map + Offset);
        if (Desc->Type == EfiConventionalMemory) {
          UINT64  Bytes = Desc->NumberOfPages * EFI_PAGE_SIZE;
          TotalConvBytes += Bytes;
          if (Desc->PhysicalStart >= 0x100000000ULL) {
            HighConvBytes += Bytes;
          }
        }
      }
    }

    if (Map != NULL) {
      FreePool (Map);
    }
  }

  Loader       = NULL;
  HasLoader    = !EFI_ERROR (gBS->LocateProtocol ((EFI_GUID *)&mUniXnuLoaderProtocolGuid, NULL, &Loader)) && (Loader != NULL);
  BootLu       = 0xFFFFFFFFU;
  BootLuStatus = AstonGetBootLu (&BootLu);
  UnixnuStatus = AstonFindGptPartition (L"unixnu", &Unixnu);
  UnixnuBlocks = EFI_ERROR (UnixnuStatus) ? 0 : (Unixnu.EndingLba - Unixnu.StartingLba + 1ULL);

  ZeroMem (Report, sizeof (Report));
  AsciiSPrint (
    Report,
    sizeof (Report),
    "ASTON_UEFI_PROBE_V1\n"
    "GOP=%ux%u FB_BASE=0x%lx FB_SIZE=0x%x\n"
    "CONV_TOTAL_MIB=%lu HIGH_4G_MIB=%lu\n"
    "UNIXNU_LOADER=%u\n"
    "BOOTLU=%u STATUS=%u\n"
    "UNIXNU_FOUND=%u START_LBA=%lu BLOCKS=%lu\n",
    (UINT32)GopW,
    (UINT32)GopH,
    FbBase,
    (UINT32)FbSize,
    TotalConvBytes >> 20,
    HighConvBytes >> 20,
    HasLoader ? 1U : 0U,
    BootLu,
    (UINT32)(BootLuStatus & 0xFFFFFFFFU),
    EFI_ERROR (UnixnuStatus) ? 0U : 1U,
    EFI_ERROR (UnixnuStatus) ? 0ULL : Unixnu.StartingLba,
    UnixnuBlocks
    );

  Print (
    L"\nASTON_UEFI_PROBE_V1 GOP=%ux%u CONV=%luMiB HIGH4G=%luMiB LOADER=%u BOOTLU=%u UNIXNU=%lu\n",
    (UINT32)GopW,
    (UINT32)GopH,
    TotalConvBytes >> 20,
    HighConvBytes >> 20,
    HasLoader ? 1U : 0U,
    BootLu,
    UnixnuBlocks
    );

  if (!EFI_ERROR (UnixnuStatus)) {
    Unixnu.BlockIo->WriteBlocks (Unixnu.BlockIo, Unixnu.MediaId, Unixnu.StartingLba, sizeof (Report), Report);
    Unixnu.BlockIo->FlushBlocks (Unixnu.BlockIo);
  }

  gBS->Stall (1500000);
  AstonRebootWithReason (ASTON_REBOOT_BOOTLOADER);
}

STATIC
VOID
EFIAPI
OnReadyToBoot (
  IN EFI_EVENT  Event,
  IN VOID       *Context
  )
{
  gBS->CloseEvent (Event);

  if (FixedPcdGet8 (PcdAstonProbeMode) != 0) {
    RunUefiHardwareProbeAndReboot ();
  } else {
    if (!mPublished) {
      if (EFI_ERROR (TryPublishUnixnuPayload ())) {
        InstallFallbackFs ();
      }
    } else if (mRamHandle != NULL) {
      gBS->ConnectController (mRamHandle, NULL, NULL, TRUE);
    }
  }
}

EFI_STATUS
EFIAPI
AstonConfigDxeEntry (
  IN EFI_HANDLE        ImageHandle,
  IN EFI_SYSTEM_TABLE  *SystemTable
  )
{
  EFI_EVENT  ReadyEvent;

  if (FixedPcdGet8 (PcdAstonProbeMode) == 0) {
    TryPublishUnixnuPayload ();
  }

  return gBS->CreateEventEx (
                EVT_NOTIFY_SIGNAL,
                TPL_CALLBACK,
                OnReadyToBoot,
                NULL,
                &gEfiEventReadyToBootGuid,
                &ReadyEvent
                );
}

/** @file
  Boot control, raw GPT A/B switching, UFS BootLU, PMIC PON reboot reason,
  and SHA-256 helpers for OnePlus Ace 3 (aston).

  SPDX-License-Identifier: BSD-2-Clause-Patent
**/

#include <Uefi.h>
#include <Uefi/UefiGpt.h>
#include <Library/BaseLib.h>
#include <Library/BaseMemoryLib.h>
#include <Library/DebugLib.h>
#include <Library/MemoryAllocationLib.h>
#include <Library/ResetSystemLib.h>
#include <Library/UefiBootServicesTableLib.h>
#include <Library/UefiRuntimeServicesTableLib.h>
#include <Library/AstonBootControlLib.h>
#include <Protocol/BlockIo.h>
#include <Protocol/EFICardInfo.h>
#include <Protocol/EFIPmicPon.h>

#define ASTON_UFS_BLOCK_SIZE  4096U
#define ASTON_MAX_GPT_ENTRIES 512U
#define ASTON_GPT_ATTR_ACTIVE (1ULL << 50)
#define ASTON_SLOT_ATTR_MASK  0x00FF000000000000ULL

STATIC CONST EFI_GUID  mInactiveSlotTypeGuid = {
  0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 }
};

typedef struct {
  CONST CHAR16  *Name;
  EFI_GUID      TypeGuid;
  UINT64        Attributes;
} ASTON_SLOT_B_SPEC;

STATIC CONST ASTON_SLOT_B_SPEC  mAstonSlotBSpecs[] = {
  // sda (4 entries)
  { L"vbmeta_system_a", { 0x1344859D, 0x3A6A, 0x4C14, { 0xA3, 0x16, 0x9E, 0x69, 0x6B, 0x3A, 0x54, 0x00 } }, 0x1000000000000000ULL },
  { L"vbmeta_system_b", { 0xFE3AB853, 0x5B66, 0x4D4A, { 0xBF, 0x85, 0x8D, 0x90, 0xAF, 0x1C, 0x2C, 0x4A } }, 0x1004000000000000ULL },
  { L"vbmeta_vendor_a", { 0x5A33B410, 0xCD1F, 0x4987, { 0xA6, 0xF3, 0x1E, 0x62, 0x82, 0xA6, 0x71, 0xD1 } }, 0x0000000000000000ULL },
  { L"vbmeta_vendor_b", { 0xFE3AB853, 0x5B66, 0x4D4A, { 0xBF, 0x85, 0x8D, 0x90, 0xAF, 0x1C, 0x2C, 0x4A } }, 0x0004000000000000ULL },
  // sdb (4 entries)
  { L"xbl_a", { 0xDEA0BA2C, 0xCBDD, 0x4805, { 0xB4, 0xF9, 0xF4, 0x28, 0x25, 0x1C, 0x3E, 0x98 } }, 0x10C0000000000000ULL },
  { L"xbl_config_a", { 0x5A325AE4, 0x4276, 0xB66D, { 0x0A, 0xDD, 0x34, 0x94, 0xDF, 0x27, 0x70, 0x6A } }, 0x00C0000000000000ULL },
  { L"multiimgqti_a", { 0x846C6F05, 0xEB46, 0x4C0A, { 0xA1, 0xA3, 0x36, 0x48, 0xEF, 0x3F, 0x9D, 0x0E } }, 0x10C0000000000000ULL },
  { L"multiimgoem_a", { 0xE126A436, 0x757E, 0x42D0, { 0x8D, 0x19, 0x0F, 0x36, 0x2F, 0x7A, 0x62, 0xB8 } }, 0x10C0000000000000ULL },
  // sdc (4 entries)
  { L"xbl_b", { 0xDEA0BA2C, 0xCBDD, 0x4805, { 0xB4, 0xF9, 0xF4, 0x28, 0x25, 0x1C, 0x3E, 0x98 } }, 0x10C4000000000000ULL },
  { L"xbl_config_b", { 0x5A325AE4, 0x4276, 0xB66D, { 0x0A, 0xDD, 0x34, 0x94, 0xDF, 0x27, 0x70, 0x6A } }, 0x00C4000000000000ULL },
  { L"multiimgqti_b", { 0x846C6F05, 0xEB46, 0x4C0A, { 0xA1, 0xA3, 0x36, 0x48, 0xEF, 0x3F, 0x9D, 0x0E } }, 0x10C4000000000000ULL },
  { L"multiimgoem_b", { 0xE126A436, 0x757E, 0x42D0, { 0x8D, 0x19, 0x0F, 0x36, 0x2F, 0x7A, 0x62, 0xB8 } }, 0x10C4000000000000ULL },
  // sdd (2 entries)
  { L"engineering_cdt_a", { 0x21A0C19C, 0x287A, 0x42FA, { 0x91, 0xE7, 0xF6, 0x4C, 0x32, 0x26, 0xA8, 0x94 } }, 0x10FB000000000000ULL },
  { L"engineering_cdt_b", { 0xB4184EDA, 0x2088, 0x4863, { 0xA1, 0x8F, 0x95, 0x76, 0xF7, 0x17, 0xAD, 0xF3 } }, 0x107F000000000000ULL },
  // sde (62 entries)
  { L"uefi_a", { 0x9F234B5B, 0x0EFB, 0x4313, { 0x8E, 0x4C, 0x0A, 0xF1, 0xF6, 0x05, 0x53, 0x6B } }, 0x10FB000000000000ULL },
  { L"aop_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"aop_config_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"tz_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"hyp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"modem_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"bluetooth_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"mdtpsecapp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"mdtp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"abl_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"dsp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"keymaster_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"boot_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x0FF3000000000000ULL },
  { L"devcfg_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"qupfw_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"vbmeta_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"dtbo_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"uefisecapp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"imagefv_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000001ULL },
  { L"shrm_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"vm-bootsys_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"cpucp_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"featenabler_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"vendor_boot_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"qweslicstore_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x10FB000000000000ULL },
  { L"oplus_sec_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"recovery_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"splash_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"xbl_ramdump_a", { 0xC3E58B09, 0xABCB, 0x42EA, { 0x9F, 0x0C, 0x3F, 0xA4, 0x53, 0xFA, 0x89, 0x2E } }, 0x00FB000000000000ULL },
  { L"rticmpdata_a", { 0x5EF416CA, 0x4198, 0x4FF9, { 0x82, 0xA5, 0x79, 0x39, 0x72, 0x67, 0x34, 0x06 } }, 0x1000000000000000ULL },
  { L"init_boot_a", { 0xE5B0F3F7, 0x6606, 0x43DA, { 0xBC, 0xE1, 0x84, 0xDC, 0x0A, 0x23, 0x9D, 0xDD } }, 0x00FB000000000000ULL },
  { L"uefi_b", { 0x400FFDCD, 0x22E0, 0x47E7, { 0x9A, 0x23, 0xF1, 0x6E, 0xD9, 0x38, 0x23, 0x88 } }, 0x107F000000000000ULL },
  { L"aop_b", { 0xD69E90A5, 0x4CAB, 0x0071, { 0xF6, 0xDF, 0xAB, 0x97, 0x7F, 0x14, 0x1A, 0x7F } }, 0x007F000000000000ULL },
  { L"aop_config_b", { 0x3D12F234, 0xC882, 0x4B46, { 0xA2, 0x0C, 0x17, 0xD5, 0x2C, 0x8F, 0xC0, 0x3D } }, 0x007F000000000000ULL },
  { L"tz_b", { 0xA053AA7F, 0x40B8, 0x4B1C, { 0xBA, 0x08, 0x2F, 0x68, 0xAC, 0x71, 0xA4, 0xF4 } }, 0x007F000000000000ULL },
  { L"hyp_b", { 0xE1A6A689, 0x0C8D, 0x4CC6, { 0xB4, 0xE8, 0x55, 0xA4, 0x32, 0x0F, 0xBD, 0x8A } }, 0x007F000000000000ULL },
  { L"modem_b", { 0xEBD0A0A2, 0xB9E5, 0x4433, { 0x87, 0xC0, 0x68, 0xB6, 0xB7, 0x26, 0x99, 0xC7 } }, 0x107F000000000000ULL },
  { L"bluetooth_b", { 0x6CB747F1, 0xC2EF, 0x4092, { 0xAD, 0xD0, 0xCA, 0x39, 0xF7, 0x9C, 0x7A, 0xF4 } }, 0x107F000000000000ULL },
  { L"mdtpsecapp_b", { 0xEA02D680, 0x8712, 0x4552, { 0xA3, 0xBE, 0xE6, 0x08, 0x78, 0x29, 0xC1, 0xE6 } }, 0x107F000000000000ULL },
  { L"mdtp_b", { 0x3878408A, 0xE263, 0x4B67, { 0xB8, 0x78, 0x63, 0x40, 0xB3, 0x5B, 0x11, 0xE3 } }, 0x107F000000000000ULL },
  { L"abl_b", { 0xBD6928A1, 0x4CE0, 0xA038, { 0x4F, 0x3A, 0x14, 0x95, 0xE3, 0xED, 0xDF, 0xFB } }, 0x107F000000000000ULL },
  { L"dsp_b", { 0x7EFE5010, 0x2A1A, 0x4A1A, { 0xB8, 0xBC, 0x99, 0x02, 0x57, 0x81, 0x35, 0x12 } }, 0x107F000000000000ULL },
  { L"keymaster_b", { 0xA11D2A7C, 0xD82A, 0x4C2F, { 0x8A, 0x01, 0x18, 0x05, 0x24, 0x0E, 0x66, 0x26 } }, 0x107F000000000000ULL },
  { L"boot_b", { 0x20117F86, 0xE985, 0x4357, { 0xB9, 0xEE, 0x37, 0x4B, 0xC1, 0xD8, 0x48, 0x7D } }, 0x0077000000000000ULL },
  { L"devcfg_b", { 0xF65D4B16, 0x343D, 0x4E25, { 0xAA, 0xFC, 0xBE, 0x99, 0xB6, 0x55, 0x6A, 0x6D } }, 0x007F000000000000ULL },
  { L"qupfw_b", { 0x21D1219F, 0x2ED1, 0x4AB4, { 0x93, 0x0A, 0x41, 0xA1, 0x6A, 0xE7, 0x5F, 0x7F } }, 0x007F000000000000ULL },
  { L"vbmeta_b", { 0x4B7A15D6, 0x322C, 0x42AC, { 0x81, 0x10, 0x88, 0xB7, 0xDA, 0x0C, 0x5D, 0x77 } }, 0x107F000000000000ULL },
  { L"dtbo_b", { 0x24D0D418, 0xD31D, 0x4D8D, { 0xAC, 0x2C, 0x4D, 0x43, 0x05, 0x18, 0x84, 0x50 } }, 0x007F000000000000ULL },
  { L"uefisecapp_b", { 0xBE8A7E08, 0x1B7A, 0x4CAE, { 0x99, 0x3A, 0xD5, 0xB7, 0xFB, 0x55, 0xB3, 0xC2 } }, 0x007F000000000000ULL },
  { L"imagefv_b", { 0x17911177, 0xC9E6, 0x4372, { 0x93, 0x3C, 0x80, 0x4B, 0x67, 0x8E, 0x66, 0x6F } }, 0x007F000000000001ULL },
  { L"shrm_b", { 0xCB74CA22, 0x2F0D, 0x4B82, { 0xA1, 0xD6, 0xC4, 0x21, 0x3F, 0x34, 0x8D, 0x73 } }, 0x107F000000000000ULL },
  { L"vm-bootsys_b", { 0x7B034C58, 0x8AFC, 0x4974, { 0x94, 0x44, 0xB0, 0x3F, 0xBE, 0xE8, 0x52, 0x36 } }, 0x007F000000000000ULL },
  { L"cpucp_b", { 0x1E8615BD, 0x6D8C, 0x41AD, { 0xB3, 0xEA, 0x50, 0xE8, 0xBF, 0x40, 0xE4, 0x3F } }, 0x107F000000000000ULL },
  { L"featenabler_b", { 0x741813D2, 0x8C87, 0x4465, { 0x8C, 0x69, 0x03, 0x2C, 0x77, 0x1C, 0xCC, 0xE7 } }, 0x007F000000000000ULL },
  { L"vendor_boot_b", { 0x6D286A7F, 0x9F1E, 0x46B3, { 0xAC, 0xF4, 0x96, 0xA0, 0x59, 0x1F, 0xE6, 0x6F } }, 0x007F000000000000ULL },
  { L"qweslicstore_b", { 0x7BAB3C93, 0x5F73, 0x4D02, { 0xB8, 0xCB, 0x5B, 0x9F, 0x89, 0x9D, 0x29, 0xA8 } }, 0x107F000000000000ULL },
  { L"oplus_sec_b", { 0xE11DDA6A, 0x651B, 0x4AB4, { 0xB8, 0xC5, 0x30, 0xB3, 0x52, 0xB4, 0x72, 0xE2 } }, 0x007F000000000000ULL },
  { L"recovery_b", { 0xD504D6DB, 0xFA92, 0x4853, { 0xB5, 0x9E, 0xC7, 0xF2, 0x92, 0xE2, 0xEA, 0x19 } }, 0x007F000000000000ULL },
  { L"splash_b", { 0xAD99F201, 0xDC71, 0x4E30, { 0x96, 0x30, 0xE1, 0x9E, 0xEF, 0x55, 0x3D, 0x1B } }, 0x007F000000000000ULL },
  { L"xbl_ramdump_b", { 0x0382F197, 0xE41F, 0x4E84, { 0xB1, 0x8B, 0x0B, 0x56, 0x4A, 0xEA, 0xD8, 0x75 } }, 0x007F000000000000ULL },
  { L"rticmpdata_b", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x1004000000000000ULL },
  { L"init_boot_b", { 0xE5B0F3F7, 0x6606, 0x43DA, { 0xBC, 0xE1, 0x84, 0xDC, 0x0A, 0x23, 0x9D, 0xDD } }, 0x007F000000000000ULL },
  // sdf (2 entries)
  { L"oplusstanvbk_a", { 0x77036CD4, 0x03D5, 0x42BB, { 0x8E, 0xD1, 0x37, 0xE5, 0xA8, 0x8B, 0xAA, 0x34 } }, 0x00FB000000000000ULL },
  { L"oplusstanvbk_b", { 0x3C2EDD33, 0x4FFA, 0x4A9F, { 0x8B, 0xFA, 0x3B, 0x69, 0x68, 0x4E, 0xDA, 0xB1 } }, 0x007F000000000000ULL },
};

STATIC CONST UINT32  mSha256K[64] = {
  0x428a2f98U, 0x71374491U, 0xb5c0fbcfU, 0xe9b5dba5U, 0x3956c25bU, 0x59f111f1U, 0x923f82a4U, 0xab1c5ed5U,
  0xd807aa98U, 0x12835b01U, 0x243185beU, 0x550c7dc3U, 0x72be5d74U, 0x80deb1feU, 0x9bdc06a7U, 0xc19bf174U,
  0xe49b69c1U, 0xefbe4786U, 0x0fc19dc6U, 0x240ca1ccU, 0x2de92c6fU, 0x4a7484aaU, 0x5cb0a9dcU, 0x76f988daU,
  0x983e5152U, 0xa831c66dU, 0xb00327c8U, 0xbf597fc7U, 0xc6e00bf3U, 0xd5a79147U, 0x06ca6351U, 0x14292967U,
  0x27b70a85U, 0x2e1b2138U, 0x4d2c6dfcU, 0x53380d13U, 0x650a7354U, 0x766a0abbU, 0x81c2c92eU, 0x92722c85U,
  0xa2bfe8a1U, 0xa81a664bU, 0xc24b8b70U, 0xc76c51a3U, 0xd192e819U, 0xd6990624U, 0xf40e3585U, 0x106aa070U,
  0x19a4c116U, 0x1e376c08U, 0x2748774cU, 0x34b0bcb5U, 0x391c0cb3U, 0x4ed8aa4aU, 0x5b9cca4fU, 0x682e6ff3U,
  0x748f82eeU, 0x78a5636fU, 0x84c87814U, 0x8cc70208U, 0x90befffaU, 0xa4506cebU, 0xbef9a3f7U, 0xc67178f2U
};

#define ROR32(x, n)  (((x) >> (n)) | ((x) << (32U - (n))))

STATIC
VOID
Sha256Compress (
  IN OUT UINT32       State[8],
  IN     CONST UINT8  Block[64]
  )
{
  UINT32  W[64];
  UINT32  A, B, C, D, E, F, G, H;
  UINT32  S0, S1, Ch, Maj, Temp1, Temp2;
  UINTN   Index;

  for (Index = 0; Index < 16; Index++) {
    W[Index] = ((UINT32)Block[Index * 4 + 0] << 24) |
               ((UINT32)Block[Index * 4 + 1] << 16) |
               ((UINT32)Block[Index * 4 + 2] << 8)  |
               ((UINT32)Block[Index * 4 + 3]);
  }

  for (Index = 16; Index < 64; Index++) {
    S0       = ROR32 (W[Index - 15], 7) ^ ROR32 (W[Index - 15], 18) ^ (W[Index - 15] >> 3);
    S1       = ROR32 (W[Index - 2], 17) ^ ROR32 (W[Index - 2], 19)  ^ (W[Index - 2] >> 10);
    W[Index] = W[Index - 16] + S0 + W[Index - 7] + S1;
  }

  A = State[0];
  B = State[1];
  C = State[2];
  D = State[3];
  E = State[4];
  F = State[5];
  G = State[6];
  H = State[7];

  for (Index = 0; Index < 64; Index++) {
    S1    = ROR32 (E, 6) ^ ROR32 (E, 11) ^ ROR32 (E, 25);
    Ch    = (E & F) ^ ((~E) & G);
    Temp1 = H + S1 + Ch + mSha256K[Index] + W[Index];
    S0    = ROR32 (A, 2) ^ ROR32 (A, 13) ^ ROR32 (A, 22);
    Maj   = (A & B) ^ (A & C) ^ (B & C);
    Temp2 = S0 + Maj;

    H = G;
    G = F;
    F = E;
    E = D + Temp1;
    D = C;
    C = B;
    B = A;
    A = Temp1 + Temp2;
  }

  State[0] += A;
  State[1] += B;
  State[2] += C;
  State[3] += D;
  State[4] += E;
  State[5] += F;
  State[6] += G;
  State[7] += H;
}

VOID
EFIAPI
AstonSha256 (
  IN  CONST VOID  *Data,
  IN  UINTN       Length,
  OUT UINT8       Digest[32]
  )
{
  UINT32       State[8] = {
    0x6a09e667U, 0xbb67ae85U, 0x3c6ef372U, 0xa54ff53aU,
    0x510e527fU, 0x9b05688cU, 0x1f83d9abU, 0x5be0cd19U
  };
  CONST UINT8  *Bytes;
  UINT8        Tail[128];
  UINTN        Offset;
  UINTN        Rem;
  UINTN        PadLen;
  UINT64       BitLen;
  UINTN        Index;

  Bytes  = (CONST UINT8 *)Data;
  Offset = 0;
  while (Offset + 64 <= Length) {
    Sha256Compress (State, Bytes + Offset);
    Offset += 64;
  }

  ZeroMem (Tail, sizeof (Tail));
  Rem = Length - Offset;
  if (Rem > 0) {
    CopyMem (Tail, Bytes + Offset, Rem);
  }

  Tail[Rem] = 0x80;
  PadLen    = (Rem + 1 + 8 <= 64) ? 64 : 128;
  BitLen    = (UINT64)Length * 8ULL;
  for (Index = 0; Index < 8; Index++) {
    Tail[PadLen - 1 - Index] = (UINT8)((BitLen >> (Index * 8)) & 0xFFU);
  }

  Sha256Compress (State, Tail);
  if (PadLen == 128) {
    Sha256Compress (State, Tail + 64);
  }

  for (Index = 0; Index < 8; Index++) {
    Digest[Index * 4 + 0] = (UINT8)((State[Index] >> 24) & 0xFFU);
    Digest[Index * 4 + 1] = (UINT8)((State[Index] >> 16) & 0xFFU);
    Digest[Index * 4 + 2] = (UINT8)((State[Index] >> 8)  & 0xFFU);
    Digest[Index * 4 + 3] = (UINT8)(State[Index] & 0xFFU);
  }
}

STATIC
CHAR16
ToLowerChar (
  IN CHAR16  Ch
  )
{
  if ((Ch >= L'A') && (Ch <= L'Z')) {
    return (CHAR16)(Ch - L'A' + L'a');
  }

  return Ch;
}

STATIC
BOOLEAN
NamesEqualNoCase (
  IN CONST CHAR16  *A,
  IN CONST CHAR16  *B
  )
{
  UINTN  Index;

  for (Index = 0; Index < 36; Index++) {
    if (ToLowerChar (A[Index]) != ToLowerChar (B[Index])) {
      return FALSE;
    }

    if (A[Index] == L'\0') {
      return TRUE;
    }
  }

  return TRUE;
}

STATIC
BOOLEAN
IsRawUfsLun (
  IN EFI_BLOCK_IO_PROTOCOL  *BlockIo
  )
{
  return (BlockIo != NULL) &&
         (BlockIo->Media != NULL) &&
         BlockIo->Media->MediaPresent &&
         !BlockIo->Media->LogicalPartition &&
         (BlockIo->Media->BlockSize == ASTON_UFS_BLOCK_SIZE) &&
         (BlockIo->Media->LastBlock > 34);
}

STATIC
EFI_STATUS
ReadGptTable (
  IN  EFI_BLOCK_IO_PROTOCOL        *BlockIo,
  IN  EFI_LBA                      HeaderLba,
  OUT EFI_PARTITION_TABLE_HEADER   *HeaderOut,
  OUT UINT8                        **EntriesOut,
  OUT UINTN                        *EntriesBufBytesOut
  )
{
  EFI_STATUS                  Status;
  UINT8                       HeaderSector[ASTON_UFS_BLOCK_SIZE];
  EFI_PARTITION_TABLE_HEADER  *Hdr;
  UINTN                       EntriesBytes;
  UINTN                       BufBytes;
  UINT8                       *Entries;

  Status = BlockIo->ReadBlocks (
                      BlockIo,
                      BlockIo->Media->MediaId,
                      HeaderLba,
                      ASTON_UFS_BLOCK_SIZE,
                      HeaderSector
                      );
  if (EFI_ERROR (Status)) {
    return Status;
  }

  Hdr = (EFI_PARTITION_TABLE_HEADER *)HeaderSector;
  if ((Hdr->Header.Signature != EFI_PTAB_HEADER_ID) ||
      (Hdr->Header.HeaderSize < sizeof (EFI_PARTITION_TABLE_HEADER)) ||
      (Hdr->SizeOfPartitionEntry < sizeof (EFI_PARTITION_ENTRY)) ||
      (Hdr->NumberOfPartitionEntries == 0) ||
      (Hdr->NumberOfPartitionEntries > ASTON_MAX_GPT_ENTRIES))
  {
    return EFI_NOT_FOUND;
  }

  EntriesBytes = (UINTN)Hdr->NumberOfPartitionEntries * (UINTN)Hdr->SizeOfPartitionEntry;
  BufBytes     = ALIGN_VALUE (EntriesBytes, ASTON_UFS_BLOCK_SIZE);
  Entries      = AllocateZeroPool (BufBytes);
  if (Entries == NULL) {
    return EFI_OUT_OF_RESOURCES;
  }

  Status = BlockIo->ReadBlocks (
                      BlockIo,
                      BlockIo->Media->MediaId,
                      Hdr->PartitionEntryLBA,
                      BufBytes,
                      Entries
                      );
  if (EFI_ERROR (Status)) {
    FreePool (Entries);
    return Status;
  }

  CopyMem (HeaderOut, Hdr, sizeof (EFI_PARTITION_TABLE_HEADER));
  *EntriesOut         = Entries;
  *EntriesBufBytesOut = BufBytes;
  return EFI_SUCCESS;
}

VOID
EFIAPI
AstonConnectAllControllers (
  VOID
  )
{
  EFI_STATUS  Status;
  EFI_HANDLE  *Handles;
  UINTN       HandleCount;
  UINTN       Index;

  Handles     = NULL;
  HandleCount = 0;
  Status      = gBS->LocateHandleBuffer (
                       AllHandles,
                       NULL,
                       NULL,
                       &HandleCount,
                       &Handles
                       );
  if (EFI_ERROR (Status) || (Handles == NULL)) {
    return;
  }

  for (Index = 0; Index < HandleCount; Index++) {
    gBS->ConnectController (Handles[Index], NULL, NULL, TRUE);
  }

  FreePool (Handles);
}

EFI_STATUS
EFIAPI
AstonFindGptPartition (
  IN  CONST CHAR16         *PartitionName,
  OUT ASTON_GPT_PARTITION  *Partition
  )
{
  EFI_STATUS                  Status;
  EFI_HANDLE                  *Handles;
  UINTN                       HandleCount;
  UINTN                       Index;
  UINTN                       Attempt;
  EFI_BLOCK_IO_PROTOCOL       *BlockIo;
  EFI_PARTITION_TABLE_HEADER  Header;
  UINT8                       *Entries;
  UINTN                       EntriesBufBytes;
  UINT32                      EntryIndex;
  EFI_PARTITION_ENTRY         *Entry;

  if ((PartitionName == NULL) || (Partition == NULL)) {
    return EFI_INVALID_PARAMETER;
  }

  Status = EFI_NOT_FOUND;
  for (Attempt = 0; Attempt < 2; Attempt++) {
    if (Attempt > 0) {
      AstonConnectAllControllers ();
    }

    Handles     = NULL;
    HandleCount = 0;
    Status      = gBS->LocateHandleBuffer (
                         ByProtocol,
                         &gEfiBlockIoProtocolGuid,
                         NULL,
                         &HandleCount,
                         &Handles
                         );
    if (EFI_ERROR (Status)) {
      continue;
    }

    for (Index = 0; Index < HandleCount; Index++) {
      Status = gBS->HandleProtocol (Handles[Index], &gEfiBlockIoProtocolGuid, (VOID **)&BlockIo);
      if (EFI_ERROR (Status) || !IsRawUfsLun (BlockIo)) {
        continue;
      }

      Entries = NULL;
      Status  = ReadGptTable (BlockIo, 1, &Header, &Entries, &EntriesBufBytes);
      if (EFI_ERROR (Status)) {
        continue;
      }

      for (EntryIndex = 0; EntryIndex < Header.NumberOfPartitionEntries; EntryIndex++) {
        Entry = (EFI_PARTITION_ENTRY *)(Entries + (UINTN)EntryIndex * Header.SizeOfPartitionEntry);
        if (IsZeroGuid (&Entry->PartitionTypeGUID)) {
          continue;
        }

        if (NamesEqualNoCase (Entry->PartitionName, PartitionName)) {
          Partition->BlockIo     = BlockIo;
          Partition->MediaId     = BlockIo->Media->MediaId;
          Partition->StartingLba = Entry->StartingLBA;
          Partition->EndingLba   = Entry->EndingLBA;
          Partition->BlockSize   = BlockIo->Media->BlockSize;
          FreePool (Entries);
          FreePool (Handles);
          return EFI_SUCCESS;
        }
      }

      FreePool (Entries);
    }

    FreePool (Handles);
    Status = EFI_NOT_FOUND;
  }

  return Status;
}

EFI_STATUS
EFIAPI
AstonGetBootLu (
  OUT UINT32  *BootLu
  )
{
  EFI_STATUS                 Status;
  EFI_HANDLE                 *Handles;
  UINTN                      Count;
  UINTN                      Index;
  UINTN                      Attempt;
  EFI_MEM_CARDINFO_PROTOCOL  *CardInfo;

  if (BootLu == NULL) {
    return EFI_INVALID_PARAMETER;
  }

  Status = EFI_NOT_FOUND;
  for (Attempt = 0; Attempt < 2; Attempt++) {
    if (Attempt > 0) {
      AstonConnectAllControllers ();
    }

    Handles = NULL;
    Count   = 0;
    Status  = gBS->LocateHandleBuffer (ByProtocol, &gEfiMemCardInfoProtocolGuid, NULL, &Count, &Handles);
    if (EFI_ERROR (Status)) {
      continue;
    }

    Status = EFI_NOT_FOUND;
    for (Index = 0; Index < Count; Index++) {
      if (!EFI_ERROR (gBS->HandleProtocol (Handles[Index], &gEfiMemCardInfoProtocolGuid, (VOID **)&CardInfo)) &&
          (CardInfo->GetBootLU != NULL))
      {
        Status = CardInfo->GetBootLU (CardInfo, BootLu);
        if (!EFI_ERROR (Status)) {
          FreePool (Handles);
          return EFI_SUCCESS;
        }
      }
    }

    FreePool (Handles);
  }

  return Status;
}

STATIC
EFI_STATUS
AstonSetBootLu (
  IN UINT32  TargetBootLu
  )
{
  EFI_STATUS                 Status;
  EFI_HANDLE                 *Handles;
  UINTN                      Count;
  UINTN                      Index;
  UINTN                      Attempt;
  EFI_MEM_CARDINFO_PROTOCOL  *CardInfo;
  BOOLEAN                    AnySuccess;
  UINT32                     CurrentBootLu;

  CurrentBootLu = 0;
  if (!EFI_ERROR (AstonGetBootLu (&CurrentBootLu)) && (CurrentBootLu == TargetBootLu)) {
    return EFI_SUCCESS;
  }

  AnySuccess = FALSE;
  Status     = EFI_UNSUPPORTED;
  for (Attempt = 0; Attempt < 2; Attempt++) {
    if (Attempt > 0) {
      AstonConnectAllControllers ();
    }

    Handles = NULL;
    Count   = 0;
    Status  = gBS->LocateHandleBuffer (ByProtocol, &gEfiMemCardInfoProtocolGuid, NULL, &Count, &Handles);
    if (EFI_ERROR (Status)) {
      continue;
    }

    Status = EFI_UNSUPPORTED;
    for (Index = 0; Index < Count; Index++) {
      if (!EFI_ERROR (gBS->HandleProtocol (Handles[Index], &gEfiMemCardInfoProtocolGuid, (VOID **)&CardInfo)) &&
          (CardInfo->SetBootLU != NULL))
      {
        Status = CardInfo->SetBootLU (CardInfo, TargetBootLu);
        if (!EFI_ERROR (Status)) {
          AnySuccess = TRUE;
          break;
        }
      }
    }

    FreePool (Handles);
    if (AnySuccess) {
      return EFI_SUCCESS;
    }
  }

  CurrentBootLu = 0;
  if (!EFI_ERROR (AstonGetBootLu (&CurrentBootLu)) && (CurrentBootLu == TargetBootLu)) {
    return EFI_SUCCESS;
  }

  return Status;
}

STATIC
VOID
BuildSlotPartnerName (
  IN  CONST CHAR16  *SourceName,
  OUT CHAR16        PartnerName[36]
  )
{
  UINTN  Len;

  ZeroMem (PartnerName, sizeof (CHAR16) * 36);
  for (Len = 0; Len < 35 && SourceName[Len] != L'\0'; Len++) {
    PartnerName[Len] = SourceName[Len];
  }

  if ((Len >= 2) && (PartnerName[Len - 2] == L'_')) {
    PartnerName[Len - 1] = L'b';
  }
}

STATIC
BOOLEAN
IsSlotNameWithSuffix (
  IN CONST CHAR16  *Name,
  IN CHAR16        Suffix
  )
{
  UINTN  Len;

  Len = 0;
  while ((Len < 36) && (Name[Len] != L'\0')) {
    Len++;
  }

  return (Len >= 3) && (Name[Len - 2] == L'_') && (ToLowerChar (Name[Len - 1]) == ToLowerChar (Suffix));
}

STATIC
BOOLEAN
IsXblName (
  IN CONST CHAR16  *Name
  )
{
  return (NamesEqualNoCase (Name, L"xbl_a") ||
          NamesEqualNoCase (Name, L"xbl_b") ||
          NamesEqualNoCase (Name, L"xbl_config_a") ||
          NamesEqualNoCase (Name, L"xbl_config_b"));
}

STATIC
CONST ASTON_SLOT_B_SPEC *
FindSlotBSpec (
  IN CONST CHAR16  *Name
  )
{
  UINTN  Index;

  for (Index = 0; Index < (sizeof (mAstonSlotBSpecs) / sizeof (mAstonSlotBSpecs[0])); Index++) {
    if (NamesEqualNoCase (Name, mAstonSlotBSpecs[Index].Name)) {
      return &mAstonSlotBSpecs[Index];
    }
  }

  return NULL;
}

STATIC
EFI_STATUS
WriteHeaderAndEntries (
  IN EFI_BLOCK_IO_PROTOCOL             *BlockIo,
  IN CONST EFI_PARTITION_TABLE_HEADER  *HeaderTemplate,
  IN EFI_LBA                           HeaderLba,
  IN EFI_LBA                           EntryLba,
  IN CONST UINT8                       *Entries,
  IN UINTN                             EntriesBufBytes,
  IN UINT32                            EntriesCrc32
  )
{
  EFI_STATUS                  Status;
  UINT8                       Sector[ASTON_UFS_BLOCK_SIZE];
  EFI_PARTITION_TABLE_HEADER  *Hdr;

  ZeroMem (Sector, sizeof (Sector));
  CopyMem (Sector, HeaderTemplate, sizeof (EFI_PARTITION_TABLE_HEADER));
  Hdr                          = (EFI_PARTITION_TABLE_HEADER *)Sector;
  Hdr->MyLBA                   = HeaderLba;
  Hdr->PartitionEntryLBA       = EntryLba;
  Hdr->PartitionEntryArrayCRC32 = EntriesCrc32;
  Hdr->Header.CRC32            = 0;
  gBS->CalculateCrc32 (Sector, Hdr->Header.HeaderSize, &Hdr->Header.CRC32);

  Status = BlockIo->WriteBlocks (BlockIo, BlockIo->Media->MediaId, EntryLba, EntriesBufBytes, (VOID *)Entries);
  if (EFI_ERROR (Status)) {
    return Status;
  }

  return BlockIo->WriteBlocks (BlockIo, BlockIo->Media->MediaId, HeaderLba, ASTON_UFS_BLOCK_SIZE, Sector);
}

EFI_STATUS
EFIAPI
AstonSwitchToSlotB (
  OUT UINT32  *UpdatedLuns OPTIONAL,
  OUT UINT32  *UpdatedEntries OPTIONAL
  )
{
  EFI_STATUS                  Status;
  EFI_STATUS                  BootLuStatus;
  EFI_HANDLE                  *Handles;
  UINTN                       Count;
  UINTN                       Index;
  EFI_BLOCK_IO_PROTOCOL       *BlockIo;
  EFI_PARTITION_TABLE_HEADER  PriHdr;
  EFI_PARTITION_TABLE_HEADER  AltHdr;
  UINT8                       *Entries;
  UINT8                       *AltEntries;
  UINTN                       BufBytes;
  UINTN                       AltBufBytes;
  UINT32                      I;
  UINT32                      J;
  EFI_PARTITION_ENTRY         *EntryA;
  EFI_PARTITION_ENTRY         *EntryB;
  CHAR16                      TargetB[36];
  EFI_GUID                    TempGuid;
  UINT32                      ChangedEntries;
  UINT32                      LunChanges;
  UINT32                      TotalLuns;
  UINT32                      TotalEntries;
  UINT32                      EntriesCrc;
  UINT64                      NewAttrA;
  UINT64                      NewAttrB;

  AstonConnectAllControllers ();
  BootLuStatus = AstonSetBootLu (2);
  DEBUG ((DEBUG_INFO, "AstonSwitchToSlotB: SetBootLU(2) -> %r\n", BootLuStatus));

  Status = gBS->LocateHandleBuffer (ByProtocol, &gEfiBlockIoProtocolGuid, NULL, &Count, &Handles);
  if (EFI_ERROR (Status)) {
    return Status;
  }

  TotalLuns    = 0;
  TotalEntries = 0;

  for (Index = 0; Index < Count; Index++) {
    if (EFI_ERROR (gBS->HandleProtocol (Handles[Index], &gEfiBlockIoProtocolGuid, (VOID **)&BlockIo)) ||
        !IsRawUfsLun (BlockIo))
    {
      continue;
    }

    Entries = NULL;
    if (EFI_ERROR (ReadGptTable (BlockIo, 1, &PriHdr, &Entries, &BufBytes))) {
      continue;
    }

    ChangedEntries = 0;
    for (I = 0; I < PriHdr.NumberOfPartitionEntries; I++) {
      CONST ASTON_SLOT_B_SPEC  *Spec;

      EntryA = (EFI_PARTITION_ENTRY *)(Entries + (UINTN)I * PriHdr.SizeOfPartitionEntry);
      if (IsZeroGuid (&EntryA->PartitionTypeGUID)) {
        continue;
      }

      Spec = FindSlotBSpec (EntryA->PartitionName);
      if (Spec != NULL) {
        LunChanges = 0;
        if (!CompareGuid (&EntryA->PartitionTypeGUID, &Spec->TypeGuid)) {
          CopyMem (&EntryA->PartitionTypeGUID, &Spec->TypeGuid, sizeof (EFI_GUID));
          LunChanges++;
        }

        if (EntryA->Attributes != Spec->Attributes) {
          EntryA->Attributes = Spec->Attributes;
          LunChanges++;
        }

        if (LunChanges > 0) {
          ChangedEntries++;
        }

        continue;
      }

      // Fallback only for unknown _a/_b partitions outside mAstonSlotBSpecs.
      if (IsXblName (EntryA->PartitionName)) {
        UINT64  Attr = EntryA->Attributes;
        if (IsSlotNameWithSuffix (EntryA->PartitionName, L'a')) {
          Attr &= ~ASTON_GPT_ATTR_ACTIVE;
        } else {
          Attr |= ASTON_GPT_ATTR_ACTIVE;
        }

        if (Attr != EntryA->Attributes) {
          EntryA->Attributes = Attr;
          ChangedEntries++;
        }

        continue;
      }

      if (!IsSlotNameWithSuffix (EntryA->PartitionName, L'a')) {
        continue;
      }

      BuildSlotPartnerName (EntryA->PartitionName, TargetB);
      if (FindSlotBSpec (TargetB) != NULL) {
        continue;
      }

      EntryB = NULL;
      for (J = 0; J < PriHdr.NumberOfPartitionEntries; J++) {
        EFI_PARTITION_ENTRY  *Cand = (EFI_PARTITION_ENTRY *)(Entries + (UINTN)J * PriHdr.SizeOfPartitionEntry);
        if (!IsZeroGuid (&Cand->PartitionTypeGUID) && NamesEqualNoCase (Cand->PartitionName, TargetB)) {
          EntryB = Cand;
          break;
        }
      }

      if (EntryB == NULL) {
        continue;
      }

      LunChanges = 0;
      if (CompareGuid (&EntryB->PartitionTypeGUID, &mInactiveSlotTypeGuid) &&
          !CompareGuid (&EntryA->PartitionTypeGUID, &mInactiveSlotTypeGuid))
      {
        CopyMem (&TempGuid, &EntryA->PartitionTypeGUID, sizeof (EFI_GUID));
        CopyMem (&EntryA->PartitionTypeGUID, &EntryB->PartitionTypeGUID, sizeof (EFI_GUID));
        CopyMem (&EntryB->PartitionTypeGUID, &TempGuid, sizeof (EFI_GUID));
        LunChanges++;
      }

      // Preserve upper/lower non-slot flags while setting _b active/bootable/priority=3
      // and clearing active bit (BIT50) + lowering priority on _a.
      NewAttrA = (EntryA->Attributes & ~ASTON_SLOT_ATTR_MASK) | ((EntryA->Attributes & ASTON_SLOT_ATTR_MASK) & ~ASTON_GPT_ATTR_ACTIVE);
      NewAttrA = (NewAttrA & ~(0x03ULL << 48)) | (0x02ULL << 48);
      NewAttrB = (EntryB->Attributes & ~ASTON_SLOT_ATTR_MASK) | (0x0077ULL << 48);

      if (NewAttrA != EntryA->Attributes) {
        EntryA->Attributes = NewAttrA;
        LunChanges++;
      }

      if (NewAttrB != EntryB->Attributes) {
        EntryB->Attributes = NewAttrB;
        LunChanges++;
      }

      if (LunChanges > 0) {
        ChangedEntries++;
      }
    }

    if (ChangedEntries > 0) {
      gBS->CalculateCrc32 (
             Entries,
             (UINTN)PriHdr.NumberOfPartitionEntries * PriHdr.SizeOfPartitionEntry,
             &EntriesCrc
             );
      WriteHeaderAndEntries (BlockIo, &PriHdr, 1, PriHdr.PartitionEntryLBA, Entries, BufBytes, EntriesCrc);

      AltEntries = NULL;
      if (!EFI_ERROR (ReadGptTable (BlockIo, PriHdr.AlternateLBA, &AltHdr, &AltEntries, &AltBufBytes))) {
        WriteHeaderAndEntries (BlockIo, &AltHdr, PriHdr.AlternateLBA, AltHdr.PartitionEntryLBA, Entries, BufBytes, EntriesCrc);
        FreePool (AltEntries);
      }

      BlockIo->FlushBlocks (BlockIo);
      TotalLuns++;
      TotalEntries += ChangedEntries;
    }

    FreePool (Entries);
  }

  FreePool (Handles);

  if (UpdatedLuns != NULL) {
    *UpdatedLuns = TotalLuns;
  }

  if (UpdatedEntries != NULL) {
    *UpdatedEntries = TotalEntries;
  }

  return EFI_ERROR (BootLuStatus) ? BootLuStatus : EFI_SUCCESS;
}

STATIC
VOID
WriteMiscBcb (
  IN CONST CHAR8  *Command
  )
{
  ASTON_GPT_PARTITION  Misc;
  UINT8                Block[ASTON_UFS_BLOCK_SIZE];
  UINTN                Index;

  if (EFI_ERROR (AstonFindGptPartition (L"misc", &Misc))) {
    return;
  }

  ZeroMem (Block, sizeof (Block));
  for (Index = 0; (Index < 31) && (Command[Index] != '\0'); Index++) {
    Block[Index] = (UINT8)Command[Index];
  }

  Misc.BlockIo->WriteBlocks (Misc.BlockIo, Misc.MediaId, Misc.StartingLba, ASTON_UFS_BLOCK_SIZE, Block);
  Misc.BlockIo->FlushBlocks (Misc.BlockIo);
}

STATIC
VOID
SetPonBootSpare (
  IN UINT8  Value
  )
{
  EFI_STATUS                  Status;
  EFI_QCOM_PMIC_PON_PROTOCOL  *PmicPon;

  Status = gBS->LocateProtocol (&gQcomPmicPonProtocolGuid, NULL, (VOID **)&PmicPon);
  if (!EFI_ERROR (Status) && (PmicPon != NULL) && (PmicPon->SetSpareReg != NULL)) {
    PmicPon->SetSpareReg (0, EFI_PM_PON_SOFT_SPARE, Value, 0xFE);
  }
}

VOID
EFIAPI
AstonRebootWithReason (
  IN UINT8  Reason
  )
{
  CONST CHAR16  *ResetStr;
  UINTN         ResetSize;

  ResetStr  = NULL;
  ResetSize = 0;

  if (Reason == ASTON_REBOOT_RECOVERY) {
    WriteMiscBcb ("boot-recovery");
    SetPonBootSpare (0x02);
    ResetStr  = L"recovery";
    ResetSize = sizeof (L"recovery");
  } else if (Reason == ASTON_REBOOT_BOOTLOADER) {
    WriteMiscBcb ("bootonce-bootloader");
    SetPonBootSpare (0x04);
    ResetStr  = L"bootloader";
    ResetSize = sizeof (L"bootloader");
  } else {
    WriteMiscBcb ("");
    SetPonBootSpare (0x00);
  }

  gRT->ResetSystem (EfiResetCold, EFI_SUCCESS, ResetSize, (VOID *)ResetStr);
  gRT->ResetSystem (EfiResetWarm, EFI_SUCCESS, ResetSize, (VOID *)ResetStr);
  ResetCold ();
}

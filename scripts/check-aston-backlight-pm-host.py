#!/usr/bin/env python3
"""Host regression and behavioral test for Phase 3b display PM, backlight, AstonPanel, ButtonProbe, and HidInputProbe.

Compiles the real C++ sources (AstonPanel.cpp, AppleARMBacklight.cpp, LegacyDisplayProbe.cpp, ButtonProbe.cpp,
HidInputProbe.cpp) with -Wall -Wextra -Werror -fsanitize=address,undefined against a deterministic in-memory
XNU/IOKit/mach host runtime in a temporary directory and checks:
  1. AstonPanel defaults gPanelOffCmd=0 (level=0 sends page 0 + DCS 0x51 00 00 only; no DCS 0x28), and
     aston-panel-off=1 opt-in emits 3 packets (DCS 0x28 on off, 0x29 on wake).
  2. Boot-time native_legacy_pm_observed(4, kIOReturnSuccess, 1) is a no-op (panel stays at initial firmware state).
  3. Display sleep via native_legacy_pm_observed(4, kIOReturnSuccess, 0) without CoreBrightness drives panel to 0
     while preserving gSavedUserLevel and IODisplayParameters.brightness.value.
  4. Exact boot3-phase3-diag failure reproduction: CoreBrightness setProperties(0) + PM(0), then 401 ms later PM(1)
     with zero CoreBrightness setProperties(>0), immediately restores AstonPanel to saved level (1638 or slider level).
  5. Wake 0-guard (< 300 ms) ignores stale async CoreBrightness setProperties(0) right after PM(1), genuine PM(0)
     still blanks immediately, slider updates while sleeping update saved level without lighting the panel until PM(1),
     and setProperties(0) >= 350 ms after PM(1) blanks normally.
  6. ButtonProbe holds kReportPower >= 150 ms on short 30 ms taps, does not extend 800 ms holds, leaves Volume taps
     unheld, and retries SPMI armPeripheral failures up to 4 attempts.
  7. HidInputProbe hasDigitizerPair retains/releases properties safely without double-free or leak.
  8. AppleARMBacklight.o places gSavedUserLevel/gDisplayPowered/gLastWakeAbs/gBacklightInstance in constant-initialized
     DATA/BSS without extra C++ dynamic global constructors (safe under --remove-section=__DATA,__mod_init_func).
"""
import argparse
import os
from pathlib import Path
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]

MOCK_HEADERS = {
    'sys/types.h': '#pragma once\n#include <stdint.h>\n#include <stddef.h>\ntypedef int kern_return_t;\n',
    'sys/proc.h': '#pragma once\ntypedef struct proc *proc_t;\nstatic inline int proc_pid(proc_t) { return 42; }\n',
    'kern/task.h': '#pragma once\ntypedef struct task *task_t;\nstatic inline void *get_bsdtask_info(task_t) { return nullptr; }\n',
    'kern/clock.h': r'''#pragma once
#include <stdint.h>
#ifndef NSEC_PER_SEC
#define NSEC_PER_SEC 1000000000ull
#endif
#ifndef NSEC_PER_MSEC
#define NSEC_PER_MSEC 1000000ull
#endif
#ifndef NSEC_PER_USEC
#define NSEC_PER_USEC 1000ull
#endif
void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs);
void absolutetime_to_nanoseconds(uint64_t abs, uint64_t *ns);
''',
    'kern/thread.h': r'''#pragma once
#include <stdint.h>
#include <sys/types.h>
typedef void *thread_t;
typedef void *event_t;
typedef int wait_result_t;
typedef void (*thread_continue_t)(void *, wait_result_t);
#define THREAD_UNINT 1
#define THREAD_AWAKENED 0
#define THREAD_CONTINUE_NULL nullptr
#define KERN_SUCCESS 0
kern_return_t kernel_thread_start(thread_continue_t cont, void *param, thread_t *thread);
void thread_deallocate(thread_t thread);
void thread_set_thread_name(thread_t thread, const char *name);
thread_t current_thread(void);
void thread_wakeup(event_t event);
wait_result_t assert_wait(event_t event, int interruptible);
wait_result_t assert_wait_timeout(event_t event, int interruptible, uint32_t interval, uint32_t scale);
void clear_wait(thread_t thread, wait_result_t result);
wait_result_t thread_block(thread_continue_t continuation);
''',
    'mach/mach_time.h': '#pragma once\n#include <stdint.h>\nuint64_t mach_absolute_time(void);\n',
    'mach/kmod.h': '#pragma once\ntypedef struct { char name[64]; } kmod_info_t;\n',
    'machine/machine_routines.h': '#pragma once\nbool ml_at_interrupt_context(void);\nbool ml_get_interrupts_enabled(void);\n',
    'pexpert/pexpert.h': r'''#pragma once
#include <stdint.h>
bool PE_parse_boot_argn(const char *arg_string, void *arg_ptr, int max_arg);
struct PE_Video { uint64_t v_baseAddr; uint32_t v_width, v_height, v_depth, v_rotate, v_rowBytes; };
int PE_current_console(struct PE_Video *info);
''',
    'IOKit/IOReturn.h': r'''#pragma once
#include <stdint.h>
typedef int IOReturn;
typedef int32_t SInt32;
typedef uint32_t UInt32;
#define kIOReturnSuccess 0
#define kIOReturnError (-1)
#define kIOReturnBadArgument (-53)
#define kIOReturnNotReady (-54)
#define kIOReturnNotAttached (-55)
''',
    'IOKit/IODeviceTreeSupport.h': '#pragma once\n#include <IOKit/IORegistryEntry.h>\n',
    'IOKit/IOLib.h': r'''#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <kern/clock.h>
#include <mach/mach_time.h>
#include <IOKit/IOReturn.h>
#include <IOKit/IOMemoryDescriptor.h>
typedef struct _IOLock { int dummy; } IOLock;
static inline IOLock *IOLockAlloc(void) { return (IOLock *)calloc(1, sizeof(IOLock)); }
static inline void IOLockLock(IOLock *) {}
static inline void IOLockUnlock(IOLock *) {}
static inline void *IOMalloc(size_t sz) { return calloc(1, sz); }
static inline __attribute__((noreturn)) void panic(const char *fmt, ...) { (void)fmt; abort(); }
void IODelay(unsigned microseconds);
void IOSleep(unsigned milliseconds);
int IOLog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
''',
    'libkern/c++/OSMetaClass.h': r'''#pragma once
#include <stddef.h>
#include <stdint.h>
class OSObject;
class OSSerialize;
class OSSymbol;
class OSMetaClass {
    const char *name_;
    uint32_t size_;
public:
    constexpr OSMetaClass(const char *name, uint32_t size = 64) : name_(name), size_(size) {}
    const char *getClassName() const { return name_; }
    uint32_t getClassSize() const { return size_; }
    static void *preModLoad(const char *) { return (void *)1; }
    static int postModLoad(void *) { return 0; }
    static const OSMetaClass *getMetaClassWithName(const OSSymbol *name);
};
class OSMetaClassBase {
public:
    virtual ~OSMetaClassBase() = default;
    virtual const OSMetaClass *getMetaClass() const;
    static OSMetaClass gMetaClass;
};
class OSObject : public OSMetaClassBase {
    mutable int refcount_ = 1;
public:
    virtual void retain() const { ++refcount_; }
    virtual void release() const { if (--refcount_ <= 0) delete this; }
    int hostRefCount() const { return refcount_; }
    virtual bool serialize(OSSerialize *) const { return false; }
    static OSMetaClass gMetaClass;
};
#define OSSafeReleaseNULL(inst) do { if (inst) { (inst)->release(); (inst) = nullptr; } } while (0)
template <typename T> T *host_dynamic_cast(const OSMetaClassBase *obj);
#define OSDynamicCast(type, inst) host_dynamic_cast<type>(inst)
#define OSTypeAlloc(type) (new type())
#define OSDeclareDefaultStructors(cls) \
public: \
    static OSMetaClass gMetaClass; \
    static const OSMetaClass *const metaClass; \
    static const OSMetaClass *const superClass; \
    cls(); \
    explicit cls(const OSMetaClass *); \
    ~cls() override; \
    const OSMetaClass *getMetaClass() const override;
#define OSDefineMetaClassAndStructors(cls, sup) \
    OSMetaClass cls::gMetaClass(#cls, sizeof(cls)); \
    const OSMetaClass *const cls::metaClass = &cls::gMetaClass; \
    const OSMetaClass *const cls::superClass = &sup::gMetaClass; \
    cls::cls() {} \
    cls::cls(const OSMetaClass *) {} \
    cls::~cls() {} \
    const OSMetaClass *cls::getMetaClass() const { return &gMetaClass; } \
    extern "C" void __GLOBAL__sub_I_Host_##cls(void) {}
''',
    'libkern/c++/OSSymbol.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <stdlib.h>
#include <string.h>
class OSSymbol : public OSObject {
    const char *str_;
public:
    explicit OSSymbol(const char *s) : str_(strdup(s)) {}
    ~OSSymbol() override { free((void *)str_); }
    const char *getCStringNoCopy() const { return str_; }
    static const OSSymbol *withCString(const char *s) { return new OSSymbol(s); }
};
''',
    'libkern/c++/OSNumber.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
class OSNumber : public OSObject {
    uint64_t value_;
public:
    explicit OSNumber(uint64_t v) : value_(v) {}
    uint32_t unsigned32BitValue() const { return (uint32_t)value_; }
    uint64_t unsigned64BitValue() const { return value_; }
    static OSNumber *withNumber(uint64_t value, unsigned) { return new OSNumber(value); }
};
''',
    'libkern/c++/OSData.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <vector>
class OSData : public OSObject {
    std::vector<uint8_t> bytes_;
public:
    OSData(const void *b, unsigned len) {
        const auto *p = static_cast<const uint8_t *>(b);
        bytes_.assign(p, p + len);
    }
    unsigned getLength() const { return (unsigned)bytes_.size(); }
    const void *getBytesNoCopy() const { return bytes_.data(); }
    static OSData *withBytes(const void *b, unsigned len) { return new OSData(b, len); }
};
''',
    'libkern/c++/OSDictionary.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <string>
#include <unordered_map>
class OSDictionary : public OSObject {
    std::unordered_map<std::string, OSObject *> map_;
public:
    ~OSDictionary() override {
        for (auto &kv : map_) kv.second->release();
    }
    static OSDictionary *withCapacity(unsigned) { return new OSDictionary(); }
    bool setObject(const char *key, OSObject *obj) {
        if (!key || !obj) return false;
        auto it = map_.find(key);
        if (it != map_.end()) {
            it->second->release();
            map_.erase(it);
        }
        obj->retain();
        map_[key] = obj;
        return true;
    }
    OSObject *getObject(const char *key) const {
        auto it = map_.find(key);
        return it == map_.end() ? nullptr : it->second;
    }
};
''',
    'libkern/c++/OSIterator.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
class OSIterator : public OSObject {
public:
    virtual OSObject *getNextObject() { return nullptr; }
};
''',
    'libkern/c++/OSSerialize.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
class OSSerialize : public OSObject {
public:
    static OSSerialize *withCapacity(unsigned) { return new OSSerialize(); }
    unsigned getLength() const { return 0; }
    const char *text() const { return ""; }
};
''',
    'libkern/c++/OSKext.h': r'''#pragma once
#include <sys/types.h>
#include <IOKit/IOReturn.h>
#include <libkern/c++/OSSerialize.h>
#define kOSKextExcludeNone 0
class OSKext {
public:
    static kern_return_t loadKextWithIdentifier(const char *, bool, bool, int, int, void *) { return 0; }
};
''',
    'IOKit/IORegistryEntry.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSIterator.h>
#include <stdio.h>
typedef const char *IOLogicalPlane;
extern const IOLogicalPlane gIOServicePlane;
extern const IOLogicalPlane gIODTPlane;
extern OSObject *const kOSBooleanTrue;
class IORegistryEntry : public OSObject {
    OSDictionary *props_ = OSDictionary::withCapacity(8);
    char name_[64] = "MockEntry";
public:
    ~IORegistryEntry() override { OSSafeReleaseNULL(props_); }
    const char *getName() const { return name_; }
    void setName(const char *n, const void * = nullptr) { snprintf(name_, sizeof(name_), "%s", n); }
    OSObject *getProperty(const char *key) const { return props_->getObject(key); }
    OSObject *copyProperty(const char *key) const {
        OSObject *v = props_->getObject(key);
        if (v) v->retain();
        return v;
    }
    bool setProperty(const char *key, OSObject *val) { return props_->setObject(key, val); }
    bool setProperty(const char *key, bool val) {
        OSNumber *n = OSNumber::withNumber(val ? 1ULL : 0ULL, 32);
        bool ok = props_->setObject(key, n);
        n->release();
        return ok;
    }
    bool setProperty(const char *key, const char *val) {
        OSObject *s = new OSObject();
        (void)val;
        bool ok = props_->setObject(key, s);
        s->release();
        return ok;
    }
    bool setProperty(const char *key, unsigned long long val, unsigned bits) {
        OSNumber *n = OSNumber::withNumber(val, bits);
        bool ok = props_->setObject(key, n);
        n->release();
        return ok;
    }
    void removeProperty(const char *) {}
    OSIterator *getChildIterator(const void *) const { return nullptr; }
    IORegistryEntry *getParentEntry(const void *) const { return nullptr; }
    bool attachToParent(IORegistryEntry *, const void *) { return true; }
    void detachFromParent(IORegistryEntry *, const void *) {}
    bool serializeProperties(OSSerialize *) const { return true; }
    bool metaCast(const char *) const { return false; }
    static IORegistryEntry *fromPath(const char *, const void *) { return nullptr; }
};
''',
    'IOKit/IOService.h': r'''#pragma once
#include <IOKit/IORegistryEntry.h>
#include <IOKit/IOLib.h>
#define kIOServiceRegisteredState 1u
class OSArray : public OSObject {
public:
    static OSArray *withCapacity(unsigned) { return new OSArray(); }
    bool setObject(OSObject *) { return true; }
};
class IOService : public IORegistryEntry {
public:
    virtual bool init() { return true; }
    virtual bool attach(IOService *);
    virtual void detach(IOService *) {}
    virtual bool start(IOService *) { return true; }
    virtual void stop(IOService *) {}
    virtual IOService *probe(IOService *, SInt32 *) { return this; }
    virtual IOReturn setProperties(OSObject *) { return kIOReturnSuccess; }
    virtual void registerService();
    uint32_t getState() const { return kIOServiceRegisteredState; }
    bool setDeviceMemory(OSArray *) { return true; }
    static IOService *getPlatform();
    static IOService *getResourceService() { return getPlatform(); }
    static OSDictionary *nameMatching(const char *) { return OSDictionary::withCapacity(1); }
    static OSDictionary *serviceMatching(const char *) { return OSDictionary::withCapacity(1); }
    static IOService *waitForMatchingService(OSDictionary *, uint64_t) { return nullptr; }
};
''',
    'IOKit/IODeviceMemory.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <IOKit/IOReturn.h>
#define kIOMapAnywhere 0u
#define kIOMapInhibitCache 0u
class IOMemoryMap : public OSObject {
    uintptr_t va_;
    uint64_t pa_;
    uint64_t len_;
public:
    IOMemoryMap(uintptr_t va, uint64_t pa, uint64_t len) : va_(va), pa_(pa), len_(len) {}
    uint64_t getVirtualAddress() const { return va_; }
    uint64_t getPhysicalAddress() const { return pa_; }
    uint64_t getLength() const { return len_; }
};
class IODeviceMemory : public OSObject {
    uint64_t base_;
    uint64_t len_;
public:
    IODeviceMemory(uint64_t base, uint64_t len) : base_(base), len_(len) {}
    static IODeviceMemory *withRange(uint64_t base, uint64_t length) { return new IODeviceMemory(base, length); }
    IOMemoryMap *map(uint32_t);
};
''',
    'IOKit/IOMemoryDescriptor.h': '#pragma once\n#include <libkern/c++/OSMetaClass.h>\nclass IOMemoryDescriptor : public OSObject {};\n',
    'IOKit/IOWorkLoop.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <IOKit/IOReturn.h>
class IOEventSource;
class IOWorkLoop : public OSObject {
public:
    typedef IOReturn (*Action)(OSObject *, void *, void *, void *, void *);
    static IOWorkLoop *workLoop() { return new IOWorkLoop(); }
    IOReturn addEventSource(IOEventSource *) { return kIOReturnSuccess; }
    IOReturn runAction(Action a, OSObject *t, void *a0 = nullptr, void *a1 = nullptr, void *a2 = nullptr, void *a3 = nullptr) {
        return a(t, a0, a1, a2, a3);
    }
};
''',
    'IOKit/IOTimerEventSource.h': r'''#pragma once
#include <libkern/c++/OSMetaClass.h>
#include <IOKit/IOReturn.h>
#include <stdint.h>
class IOEventSource : public OSObject {};
typedef uint64_t AbsoluteTime;
class IOTimerEventSource : public IOEventSource {
public:
    typedef void (*Action)(OSObject *, IOTimerEventSource *);
    OSObject *owner_;
    Action action_;
    bool armed_ = false;
    uint64_t deadlineNs_ = 0;
    IOTimerEventSource(OSObject *o, Action a);
    static IOTimerEventSource *timerEventSource(OSObject *o, Action a);
    IOReturn setTimeoutUS(uint32_t us);
    IOReturn setTimeoutMS(uint32_t ms);
    IOReturn wakeAtTime(AbsoluteTime abstime);
    void cancelTimeout();
};
''',
    'IOKit/IOInterruptEventSource.h': r'''#pragma once
#include <IOKit/IOTimerEventSource.h>
class IOService;
class IOInterruptEventSource : public IOEventSource {
public:
    typedef void (*Action)(OSObject *, IOInterruptEventSource *, int);
    static IOInterruptEventSource *interruptEventSource(OSObject *, Action) { return new IOInterruptEventSource(); }
    void interruptOccurred(void *, void *, int) {}
};
''',
    'IOKit/IOPlatformExpert.h': '#pragma once\n#include <IOKit/IOService.h>\n',
    'IOKit/IOUserClient.h': r'''#pragma once
#include <IOKit/IOService.h>
struct IOExternalMethodDispatch;
struct IOExternalMethodArguments {
    const uint64_t *scalarInput;
    uint32_t scalarInputCount;
};
class IOUserClient : public IOService {};
''',
}

HARNESS_CPP = r'''
#include <cassert>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include <pexpert/pexpert.h>
#include <IOKit/IOService.h>
#include <IOKit/IODeviceMemory.h>
#include <IOKit/IOTimerEventSource.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSDictionary.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSSymbol.h>
#include <kern/clock.h>
#include <kern/thread.h>
#include <mach/kmod.h>
#include "BootResetCore.h"
#include "GeniI2c.h"

extern "C" {
volatile int32_t native_backlight_stats[16];
extern volatile int32_t aston_panel_stats[12];
extern volatile int32_t native_backlight_request[6];
extern volatile int32_t native_buttons_stats[10];

int aston_panel_open(void);
int aston_panel_level(uint32_t level);
void aston_backlight_start(void);
void sheng_backlight_display_power(bool powered);
void native_legacy_pm_observed(unsigned stage, IOReturn status, unsigned long ordinal);
void native_buttons_set_sink(void (*sink)(void *, uint32_t), void *context);
void sheng_buttons_probe(void);
bool sheng_buttons_press(uint32_t mask, uint32_t ms);

kmod_info_t g_kernel_kmod_info = {"com.unixnu.hosttest"};
}

// ---------- Virtual time & IOKit / XNU mock runtime ----------
static uint64_t gNowNs = 10'000'000'000ull; // start at 10 s
static std::vector<IOTimerEventSource *> gAllTimers;
static IOService *gPlatform = nullptr;
static IOService *gRegisteredBacklight = nullptr;

static bool gBootArgAstonPanelOffSet = false;
static unsigned gBootArgAstonPanelOff = 0;
static unsigned gBootArgShengButtons = 0;
static bool gBootArgHoldMsSet = false;
static unsigned gBootArgHoldMs = 0;

static uint32_t gMockDsiRegs[0x1000 / 4];
static uint32_t gMockCore[0x3000 / 4];
static uint32_t gMockCnfg[0x4000 / 4];
static uint32_t gMockObs[0x20000 / 4];
static uint32_t gMockChn[0x20000 / 4];
static uint32_t gMockIntr[0x1000 / 4];
static uint32_t gMockPdc[0x2000 / 4];

const IOLogicalPlane gIOServicePlane = "IOService";
const IOLogicalPlane gIODTPlane = "IODeviceTree";
static OSObject gTrueObj;
OSObject *const kOSBooleanTrue = &gTrueObj;

OSMetaClass OSMetaClassBase::gMetaClass("OSMetaClassBase");
OSMetaClass OSObject::gMetaClass("OSObject");
const OSMetaClass *OSMetaClassBase::getMetaClass() const { return &gMetaClass; }
const OSMetaClass *OSMetaClass::getMetaClassWithName(const OSSymbol *) {
    // Host mock omits 320-slot XNU kernel vtable copy when __has_feature(ptrauth_calls) is 0.
    return nullptr;
}

template <> OSNumber *host_dynamic_cast<OSNumber>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<OSNumber *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> OSData *host_dynamic_cast<OSData>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<OSData *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> OSDictionary *host_dynamic_cast<OSDictionary>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<OSDictionary *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> IOService *host_dynamic_cast<IOService>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<IOService *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> IORegistryEntry *host_dynamic_cast<IORegistryEntry>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<IORegistryEntry *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}

bool IOService::attach(IOService *) {
    retain();
    return true;
}
void IOService::registerService() {
    gRegisteredBacklight = this;
}
IOService *IOService::getPlatform() {
    if (!gPlatform) gPlatform = new IOService();
    return gPlatform;
}

IOMemoryMap *IODeviceMemory::map(uint32_t) {
    uintptr_t va = 0;
    if (base_ == 0x0ae94000) va = reinterpret_cast<uintptr_t>(gMockDsiRegs);
    else if (base_ == 0xc400000) va = reinterpret_cast<uintptr_t>(gMockCore);
    else if (base_ == 0xc42d000) va = reinterpret_cast<uintptr_t>(gMockCnfg);
    else if (base_ == 0xc440000) va = reinterpret_cast<uintptr_t>(gMockObs);
    else if (base_ == 0xc500000) va = reinterpret_cast<uintptr_t>(gMockChn);
    else if (base_ == 0xc4c0000) va = reinterpret_cast<uintptr_t>(gMockIntr);
    else if (base_ == 0xb220000) va = reinterpret_cast<uintptr_t>(gMockPdc);
    return new IOMemoryMap(va, base_, len_);
}

IOTimerEventSource::IOTimerEventSource(OSObject *o, Action a) : owner_(o), action_(a) {
    gAllTimers.push_back(this);
}
IOTimerEventSource *IOTimerEventSource::timerEventSource(OSObject *o, Action a) {
    return new IOTimerEventSource(o, a);
}
IOReturn IOTimerEventSource::setTimeoutUS(uint32_t us) {
    armed_ = true;
    deadlineNs_ = gNowNs + (uint64_t)us * 1000ull;
    return kIOReturnSuccess;
}
IOReturn IOTimerEventSource::setTimeoutMS(uint32_t ms) {
    armed_ = true;
    deadlineNs_ = gNowNs + (uint64_t)ms * 1000000ull;
    return kIOReturnSuccess;
}
IOReturn IOTimerEventSource::wakeAtTime(AbsoluteTime abstime) {
    armed_ = true;
    deadlineNs_ = abstime;
    return kIOReturnSuccess;
}
void IOTimerEventSource::cancelTimeout() { armed_ = false; }

static void flushWorkLoopTimers(uint64_t maxWaitMs = 100) {
    const uint64_t endNs = gNowNs + maxWaitMs * 1000000ull;
    for (;;) {
        IOTimerEventSource *soonest = nullptr;
        for (IOTimerEventSource *t : gAllTimers) {
            if (!t->armed_ || t->deadlineNs_ > endNs) continue;
            if (!soonest || t->deadlineNs_ < soonest->deadlineNs_) soonest = t;
        }
        if (!soonest) break;
        if (soonest->deadlineNs_ > gNowNs) gNowNs = soonest->deadlineNs_;
        soonest->armed_ = false;
        soonest->action_(soonest->owner_, soonest);
    }
}

uint64_t mach_absolute_time(void) { return gNowNs; }
void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs) { *abs = ns; }
void absolutetime_to_nanoseconds(uint64_t abs, uint64_t *ns) { *ns = abs; }

bool PE_parse_boot_argn(const char *arg_string, void *arg_ptr, int max_arg) {
    if (!strcmp(arg_string, "aston-panel-off") && gBootArgAstonPanelOffSet && max_arg >= (int)sizeof(unsigned)) {
        *static_cast<unsigned *>(arg_ptr) = gBootArgAstonPanelOff;
        return true;
    }
    if (!strcmp(arg_string, "sheng-buttons") && gBootArgShengButtons && max_arg >= (int)sizeof(unsigned)) {
        *static_cast<unsigned *>(arg_ptr) = gBootArgShengButtons;
        return true;
    }
    if (!strcmp(arg_string, "sheng-buttons-hold-ms") && gBootArgHoldMsSet && max_arg >= (int)sizeof(unsigned)) {
        *static_cast<unsigned *>(arg_ptr) = gBootArgHoldMs;
        return true;
    }
    return false;
}
int PE_current_console(struct PE_Video *) { return -1; }
bool ml_at_interrupt_context(void) { return false; }
bool ml_get_interrupts_enabled(void) { return true; }

static unsigned gIODelayCount = 0;
static unsigned gDelayFailureInjectRemaining = 0;
static void setAllSpmiStatus(uint32_t status) {
    for (int apid = 0; apid < 8; ++apid) {
        gMockObs[(BR_OBS_APID_STRIDE * (uint32_t)apid + BR_STATUS) / 4] = status;
        gMockChn[(BR_CHNL_APID_STRIDE * (uint32_t)apid + BR_STATUS) / 4] = status;
    }
}
void IODelay(unsigned microseconds) {
    if (microseconds == 1000) {
        ++gIODelayCount;
        if (gDelayFailureInjectRemaining > 0) {
            --gDelayFailureInjectRemaining;
            if (gDelayFailureInjectRemaining == 0) setAllSpmiStatus(BR_STATUS_DONE);
        }
    }
}
void IOSleep(unsigned milliseconds) { gNowNs += (uint64_t)milliseconds * 1000000ull; }
int IOLog(const char *, ...) { return 0; }

// ---------- Thread / SPMI / Buttons mock ----------
struct StopButtonsTest {};
static thread_continue_t gStartedThread = nullptr;
static void *gStartedParam = nullptr;
static void (*gAttachedSpiIrq)(void *) = nullptr;
static bool gLastWaitTimed = false;
static uint32_t gLastWaitTimeoutUs = 0;
static int gWaitStep = 0;

struct ButtonReport {
    uint64_t timeMs;
    uint32_t mask;
};
static std::vector<ButtonReport> gButtonReports;
static void recordButtonSink(void *, uint32_t mask) {
    gButtonReports.push_back({gNowNs / 1000000ull, mask});
}

extern "C" int armcpu_spi_attach(uint32_t irq, bool, void (*fn)(void *), void *, uint32_t *, uint64_t *) {
    if (irq == 32 + 481) gAttachedSpiIrq = fn;
    return kIOReturnSuccess;
}
kern_return_t kernel_thread_start(thread_continue_t cont, void *param, thread_t *thread) {
    gStartedThread = cont;
    gStartedParam = param;
    if (thread) *thread = (thread_t)1;
    return KERN_SUCCESS;
}
void thread_deallocate(thread_t) {}
void thread_set_thread_name(thread_t, const char *) {}
thread_t current_thread(void) { return (thread_t)1; }
void thread_wakeup(event_t) {}
wait_result_t assert_wait(event_t, int) {
    gLastWaitTimed = false;
    gLastWaitTimeoutUs = 0;
    return 0;
}
wait_result_t assert_wait_timeout(event_t, int, uint32_t interval, uint32_t scale) {
    gLastWaitTimed = true;
    gLastWaitTimeoutUs = (uint32_t)(((uint64_t)interval * scale) / 1000ull);
    return 0;
}
void clear_wait(thread_t, wait_result_t) {}

static void setPonGpioRt(bool powerDown, bool volUpDown, bool volDownDown) {
    uint8_t pon = 0;
    if (powerDown) pon |= (1u << 7);
    if (volUpDown) pon |= (1u << 6); // gVolumeSwap == 1 -> RESIN = Volume Up
    uint8_t gpio = volDownDown ? 0u : (1u << 0); // active low
    gMockObs[(BR_OBS_APID_STRIDE * 1u + BR_RDATA0) / 4] = pon;
    gMockObs[(BR_OBS_APID_STRIDE * 2u + BR_RDATA0) / 4] = gpio;
}

static void raisePonIrq() {
    gMockIntr[0] = (1u << 1);
    gMockChn[(BR_CHNL_APID_STRIDE * 1u + 0x104) / 4] = 1u << 7;
    assert(gAttachedSpiIrq != nullptr);
    gAttachedSpiIrq(nullptr);
    gMockIntr[0] = 0;
}

wait_result_t thread_block(thread_continue_t) {
    // Timeline inside buttonsThread():
    //   step 0: initially idle -> t=20000ms assert power (30 ms short tap begins)
    //   step 1: advance +30 ms -> release power hardware
    //   step 2: buttonsThread computes remainingUs == 120000 -> advance by remainingUs -> expires at 150 ms
    //   step 3: at t=20200ms assert power (800 ms long hold begins)
    //   step 4: at t=21000ms release power hardware (already held >= 150 ms)
    //   step 5: at t=21200ms assert volume-up (30 ms tap begins) -> assert NO timeout wait on release
    //   step 6: at t=21230ms release volume-up hardware -> must release immediately!
    //   step 7: terminate scripted loop cleanly via C++ exception.
    switch (gWaitStep++) {
    case 0:
        assert(!gLastWaitTimed);
        gNowNs = 20'000'000'000ull;
        setPonGpioRt(true, false, false);
        raisePonIrq();
        break;
    case 1:
        assert(!gLastWaitTimed);
        gNowNs = 20'030'000'000ull;
        setPonGpioRt(false, false, false);
        raisePonIrq();
        break;
    case 2:
        assert(gLastWaitTimed);
        assert(gLastWaitTimeoutUs >= 119000 && gLastWaitTimeoutUs <= 121000);
        gNowNs += (uint64_t)gLastWaitTimeoutUs * 1000ull;
        break;
    case 3:
        assert(!gLastWaitTimed);
        gNowNs = 20'200'000'000ull;
        setPonGpioRt(true, false, false);
        raisePonIrq();
        break;
    case 4:
        assert(!gLastWaitTimed);
        gNowNs = 21'000'000'000ull;
        setPonGpioRt(false, false, false);
        raisePonIrq();
        break;
    case 5:
        assert(!gLastWaitTimed);
        gNowNs = 21'200'000'000ull;
        setPonGpioRt(false, true, false);
        raisePonIrq();
        break;
    case 6:
        assert(!gLastWaitTimed);
        gNowNs = 21'230'000'000ull;
        setPonGpioRt(false, false, false);
        raisePonIrq();
        break;
    default:
        assert(!gLastWaitTimed);
        throw StopButtonsTest{};
    }
    return 0;
}

// ---------- Stubs for unused LegacyDisplayProbe / HidInputProbe kernel hooks ----------
extern "C" {
int geni_i2c_prepare(GeniI2c *, const GeniIo *, uint8_t, uint32_t *) { return -1; }
int geni_i2c_start_write(GeniI2c *, uint8_t, uint8_t) { return -1; }
int geni_i2c_start_read(GeniI2c *, uint8_t) { return -1; }
int geni_i2c_event(GeniI2c *, uint32_t) { return GeniFailed; }
int geni_i2c_timeout(GeniI2c *) { return GeniFailed; }

void host_nld_init(void) __asm__("__GLOBAL__sub_I_NativeLegacyDisplay.cpp");
void host_nld_init(void) {}
IOService *native_legacy_display_allocate(void) { return nullptr; }
void host_ngp_init(void) __asm__("__GLOBAL__sub_I_NativeGOPPresenter.cpp");
void host_ngp_init(void) {}
IOService *native_gop_presenter_create(void) { return nullptr; }
bool native_gop_gpu_offer(IOService *, void *, IOMemoryDescriptor *, uint64_t, uint32_t, uint32_t, uint32_t) { return false; }
void native_gop_presenter_set_source(IOService *, IOService *) {}
int boot_progress_pulse_anchor(uintptr_t, uint64_t, unsigned, unsigned, unsigned) { return 0; }
int boot_progress_prepare_scanout(void) { return 1; }
void boot_progress_cancel_scanout(void) {}
void boot_progress_stage(const char *) {}
kern_return_t OSKextLoadKextWithIdentifier(const char *) { return -1; }
void native_keystore_probe(void) {}
void native_touch_probe(void) {}
void sheng_cpufreq_probe(void) {}
void sheng_gpu_power_probe(void) {}
void sheng_wifi_probe(void) {}
void sheng_bluetooth_probe(void) {}
void sheng_usb_probe(void) {}
void sheng_adsp_probe(void) {}
void sheng_battery_probe(void) {}
void sheng_audio_probe(void) {}
void sheng_backlight_probe(void) {}
void sheng_dpu_probe(void) {}
void aston_boot_screen_probe(void) {}
void aston_halt_restart_install(void) {}
void aston_display_window_probe(void) {}
void aston_storage_traits_install(void) {}
void aston_dpu_survey_probe(void) {}

void host_nhi_init(void) __asm__("__GLOBAL__sub_I_NativeHidInput.cpp");
void host_nhi_init(void) {}
IOService *native_hid_input_allocate(void) { return nullptr; }
IOService *native_hid_touch_allocate(void) { return nullptr; }
}

// Helper to inspect IODisplayParameters.brightness.value
static uint32_t readPublishedBrightness(IOService *svc) {
    auto *params = OSDynamicCast(OSDictionary, svc->getProperty("IODisplayParameters"));
    assert(params != nullptr);
    auto *br = OSDynamicCast(OSDictionary, params->getObject("brightness"));
    assert(br != nullptr);
    auto *val = OSDynamicCast(OSNumber, br->getObject("value"));
    assert(val != nullptr);
    return val->unsigned32BitValue();
}

static void sendBrightnessProp(IOService *svc, int32_t level, uint32_t fadeSecondsFixed = 0) {
    auto *dict = OSDictionary::withCapacity(2);
    auto *b = OSNumber::withNumber((uint64_t)(uint32_t)level, 32);
    auto *f = OSNumber::withNumber(fadeSecondsFixed, 32);
    dict->setObject("brightness", b);
    dict->setObject("FadePeriod", f);
    b->release();
    f->release();
    IOReturn rc = svc->setProperties(dict);
    dict->release();
    assert(rc == kIOReturnSuccess);
}

static void runDisplayPmTests() {
    // Prepare DSI0 controller registers: enabled (bit 0) + DMA done always ready (bit 0)
    memset(gMockDsiRegs, 0, sizeof(gMockDsiRegs));
    gMockDsiRegs[0x004 / 4] = 1u; // kCtrl bit 0
    gMockDsiRegs[0x110 / 4] = 1u; // kIntCtrl DMA done

    // Start panel & backlight (calls aston_panel_open & startPanel -> publishFirst -> registerService)
    aston_backlight_start();
    assert(gRegisteredBacklight != nullptr);
    assert(aston_panel_stats[0] == 1);
    assert(native_backlight_stats[0] == 3 || native_backlight_stats[0] == 4);
    assert(readPublishedBrightness(gRegisteredBacklight) == 1638);
    assert(aston_panel_stats[6] == 0);

    // 1. Boot-time PM(1) must be a no-op because firmware panel is already lit
    native_legacy_pm_observed(4, kIOReturnSuccess, 1);
    flushWorkLoopTimers();
    assert(aston_panel_stats[6] == 0);

    // 2. Sleep via PM(0) with zero CoreBrightness calls: drives panel to 0 (2 packets, NO DCS 0x28!)
    native_legacy_pm_observed(4, kIOReturnSuccess, 0);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 0);
    assert(aston_panel_stats[6] == 2);                // default gPanelOffCmd == 0 -> only 2 packets (page 0 + DCS 0x51)
    assert(gMockDsiRegs[0x17c / 4] == 0xff000051u);   // kTpgInit last word is DCS 0x51 00 00
    assert(readPublishedBrightness(gRegisteredBacklight) == 1638);

    // 3. Exact boot3-phase3-diag scenario: CoreBrightness ALSO sends brightness=0 during sleep, then 401 ms later PM(1) with NO setProperties(>0)
    sendBrightnessProp(gRegisteredBacklight, 0);
    flushWorkLoopTimers();
    assert(readPublishedBrightness(gRegisteredBacklight) == 1638); // goal==0 must never clobber IODisplayParameters!

    gNowNs += 401'000'000ull; // +401 ms
    native_legacy_pm_observed(4, kIOReturnSuccess, 1);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 1638);
    assert(aston_panel_stats[6] == 4);                // +2 packets for wake (page 0 + DCS 0x51 06 66)
    assert(gMockDsiRegs[0x17c / 4] == 0xff660651u);   // DCS 0x51 0x06 0x66
    assert(readPublishedBrightness(gRegisteredBacklight) == 1638);

    // 4. Stale async CoreBrightness brightness=0 arriving 50 ms after wake must be ignored (< 300 ms wake guard)
    gNowNs += 50'000'000ull;
    sendBrightnessProp(gRegisteredBacklight, 0);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 1638);
    assert(aston_panel_stats[6] == 4);

    // 5. However, genuine PM(0) arriving only 50 ms after wake must STILL blank immediately!
    gNowNs += 10'000'000ull;
    native_legacy_pm_observed(4, kIOReturnSuccess, 0);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 0);
    assert(aston_panel_stats[6] == 6);

    // 6. Slider update while display is sleeping must save new level without lighting panel until PM(1)
    gNowNs += 500'000'000ull;
    sendBrightnessProp(gRegisteredBacklight, 2400);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 0);
    assert(aston_panel_stats[6] == 6);
    assert(readPublishedBrightness(gRegisteredBacklight) == 2400);

    // Wake restores new slider level 2400 (0x0960) even without new setProperties
    gNowNs += 100'000'000ull;
    native_legacy_pm_observed(4, kIOReturnSuccess, 1);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 2400);
    assert(aston_panel_stats[6] == 8);
    assert(gMockDsiRegs[0x17c / 4] == 0xff600951u); // DCS 0x51 0x09 0x60

    // 7. After >= 350 ms awake, CoreBrightness setProperties(0) can dim to 0 without overwriting saved level
    gNowNs += 350'000'000ull;
    sendBrightnessProp(gRegisteredBacklight, 0);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 0);
    assert(aston_panel_stats[6] == 10);
    assert(readPublishedBrightness(gRegisteredBacklight) == 2400);

    // Clean full cycle: sleep while dimmed -> wake restores 2400
    gNowNs += 50'000'000ull;
    native_legacy_pm_observed(4, kIOReturnSuccess, 0);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 0);
    // forceNextWrite=false? Wait: here handleDisplayPower(false) sets forceNextWrite=true, so it sends 0 safely
    gNowNs += 500'000'000ull;
    native_legacy_pm_observed(4, kIOReturnSuccess, 1);
    flushWorkLoopTimers();
    assert(aston_panel_stats[9] == 2400);

    puts("PASS [1/4]: AstonPanel default + LegacyDisplay PM <-> AppleARMBacklight synchronization & wake guard");
}

static void runButtonsTests() {
    gBootArgShengButtons = 1;
    memset(gMockCore, 0, sizeof(gMockCore));
    memset(gMockCnfg, 0, sizeof(gMockCnfg));
    memset(gMockObs, 0, sizeof(gMockObs));
    memset(gMockChn, 0, sizeof(gMockChn));
    memset(gMockIntr, 0, sizeof(gMockIntr));
    memset(gMockPdc, 0, sizeof(gMockPdc));

    gMockCore[0] = BR_VERSION_V7_MIN;
    gMockCore[BR_FEATURES / 4] = 8;
    // APID 1 -> PON (0x013, irq owner EE 0), APID 2 -> GPIO (0x18d, irq owner EE 0)
    gMockCore[(BR_APID_MAP + 4 * 1) / 4] = (1u << 24) | (0x013u << 8) | 1u;
    gMockCnfg[(BR_CNFG_OWNER + 4 * 1) / 4] = 0u;
    gMockCore[(BR_APID_MAP + 4 * 2) / 4] = (1u << 24) | (0x18du << 8) | 1u;
    gMockCnfg[(BR_CNFG_OWNER + 4 * 2) / 4] = 0u;
    gMockPdc[0x1000 / 4] = 0x30200u;

    // Force 2 SPMI failures before 3rd attempt succeeds during armPeripheral(gPon)
    setAllSpmiStatus(BR_STATUS_DONE | 2u);
    setPonGpioRt(false, false, false);
    gIODelayCount = 0;
    gDelayFailureInjectRemaining = 2;

    native_buttons_set_sink(recordButtonSink, nullptr);
    sheng_buttons_probe();
    assert(gStartedThread != nullptr);

    try {
        gStartedThread(gStartedParam, 0);
        assert(false && "buttonsThread should only exit via StopButtonsTest in host harness");
    } catch (const StopButtonsTest &) {
    }

    assert(gIODelayCount == 2); // armPeripheral retried twice before succeeding
    assert(native_buttons_stats[0] == 3);
    assert(gButtonReports.size() == 6);

    // 30 ms short Power tap -> held to 150 ms
    assert(gButtonReports[0].timeMs == 20000 && gButtonReports[0].mask == (1u << 1));
    assert(gButtonReports[1].timeMs == 20150 && gButtonReports[1].mask == 0);
    // 800 ms long Power hold -> releases immediately at 800 ms (no extra hold)
    assert(gButtonReports[2].timeMs == 20200 && gButtonReports[2].mask == (1u << 1));
    assert(gButtonReports[3].timeMs == 21000 && gButtonReports[3].mask == 0);
    // 30 ms short Volume Up tap -> releases immediately at 30 ms (unheld)
    assert(gButtonReports[4].timeMs == 21200 && gButtonReports[4].mask == (1u << 2));
    assert(gButtonReports[5].timeMs == 21230 && gButtonReports[5].mask == 0);

    puts("PASS [2/4]: ButtonProbe 150ms kReportPower-only hold, immediate Volume, and armPeripheral 4x retry");
}

int main() {
    runDisplayPmTests();
    runButtonsTests();
    return 0;
}
'''

EXTRA_TESTS_CPP = r'''
// Separate standalone binary to verify HidInputProbe::hasDigitizerPair copyProperty retention AND AstonPanel opt-in.
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <pexpert/pexpert.h>
#include <IOKit/IOService.h>
#include <IOKit/IODeviceMemory.h>
#include <libkern/c++/OSData.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSSymbol.h>
#include <kern/thread.h>
#include <mach/kmod.h>

extern "C" {
volatile uint32_t native_legacy_transfer_calls = 0;
kmod_info_t g_kernel_kmod_info = {"com.unixnu.hosttest2"};
extern volatile int32_t aston_panel_stats[12];
int aston_panel_open(void);
int aston_panel_level(uint32_t level);
}

// Re-use minimal mocks
const IOLogicalPlane gIOServicePlane = "IOService";
const IOLogicalPlane gIODTPlane = "IODeviceTree";
static OSObject gTrueObj;
OSObject *const kOSBooleanTrue = &gTrueObj;
OSMetaClass OSMetaClassBase::gMetaClass("OSMetaClassBase");
OSMetaClass OSObject::gMetaClass("OSObject");
const OSMetaClass *OSMetaClassBase::getMetaClass() const { return &gMetaClass; }
const OSMetaClass *OSMetaClass::getMetaClassWithName(const OSSymbol *) { return nullptr; }
template <> OSNumber *host_dynamic_cast<OSNumber>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<OSNumber *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> OSData *host_dynamic_cast<OSData>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<OSData *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> IOService *host_dynamic_cast<IOService>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<IOService *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
template <> IORegistryEntry *host_dynamic_cast<IORegistryEntry>(const OSMetaClassBase *o) {
    return o ? dynamic_cast<IORegistryEntry *>(const_cast<OSMetaClassBase *>(o)) : nullptr;
}
bool IOService::attach(IOService *) { retain(); return true; }
void IOService::registerService() {}
IOService *IOService::getPlatform() { static IOService p; return &p; }
static uint32_t gHostDsi[0x1000 / 4];
IOMemoryMap *IODeviceMemory::map(uint32_t) {
    return new IOMemoryMap(reinterpret_cast<uintptr_t>(gHostDsi), base_, len_);
}
uint64_t mach_absolute_time(void) { return 1'000'000'000ull; }
void nanoseconds_to_absolutetime(uint64_t ns, uint64_t *abs) { *abs = ns; }
void absolutetime_to_nanoseconds(uint64_t abs, uint64_t *ns) { *ns = abs; }
void IODelay(unsigned) {}
void IOSleep(unsigned) {}
int IOLog(const char *, ...) { return 0; }
bool PE_parse_boot_argn(const char *arg, void *ptr, int sz) {
    if (!strcmp(arg, "aston-panel-off") && sz >= (int)sizeof(unsigned)) {
        *static_cast<unsigned *>(ptr) = 1;
        return true;
    }
    return false;
}
extern "C" {
kern_return_t OSKextLoadKextWithIdentifier(const char *) { return -1; }
void host_nhi_init(void) __asm__("__GLOBAL__sub_I_NativeHidInput.cpp");
void host_nhi_init(void) {}
IOService *native_hid_input_allocate(void) { return nullptr; }
IOService *native_hid_touch_allocate(void) { return nullptr; }
}
kern_return_t kernel_thread_start(thread_continue_t, void *, thread_t *) { return -1; }
void thread_deallocate(thread_t) {}

#include "package/HidInputProbe.cpp"

int main() {
    memset(gHostDsi, 0, sizeof(gHostDsi));
    gHostDsi[0x004 / 4] = 1u;
    gHostDsi[0x110 / 4] = 1u;
    assert(aston_panel_open() == 0);
    assert(aston_panel_level(0) == 0);
    assert(aston_panel_stats[6] == 3); // aston-panel-off=1 sends page 0 + 0x51 + 0x28 (3 packets)
    assert(aston_panel_level(1638) == 0);
    assert(aston_panel_stats[6] == 6); // wake sends 0x29 + page 0 + 0x51 (3 packets)

    auto *driver = new IORegistryEntry();
    driver->setProperty("PrimaryUsagePage", 0x0dULL, 32);
    driver->setProperty("PrimaryUsage", 4ULL, 32);
    auto *pBefore = static_cast<OSNumber *>(driver->getProperty("PrimaryUsagePage"));
    int refsBefore = pBefore->hostRefCount();
    assert(hasDigitizerPair(driver));
    assert(pBefore->hostRefCount() == refsBefore);
    driver->setProperty("PrimaryUsagePage", 1ULL, 32);
    assert(!hasDigitizerPair(driver));
    driver->release();

    puts("PASS [3/4]: AstonPanel aston-panel-off=1 opt-in & HidInputProbe copyProperty refcount safety");
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--cxx', default='clang++')
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix='aston-pm-host-') as td:
        tmp = Path(td)
        inc = tmp / 'mock_inc'
        for rel, body in MOCK_HEADERS.items():
            p = inc / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(body)

        harness_cpp = tmp / 'harness.cpp'
        harness_cpp.write_text(HARNESS_CPP)
        extra_cpp = tmp / 'extra.cpp'
        extra_cpp.write_text(EXTRA_TESTS_CPP)

        cflags = [
            '-std=c++17', '-Wall', '-Wextra', '-Werror', '-O1',
            '-fsanitize=address,undefined', '-fno-sanitize=vptr',
            '-I', str(inc), '-I', str(ROOT / 'package'), '-I', str(ROOT),
        ]

        # Compile each real translation unit cleanly to an object file first so -Wall -Wextra -Werror checks them all.
        sources = [
            ROOT / 'package/AstonPanel.cpp',
            ROOT / 'package/AppleARMBacklight.cpp',
            ROOT / 'package/LegacyDisplayProbe.cpp',
            ROOT / 'package/ButtonProbe.cpp',
            ROOT / 'package/HidInputProbe.cpp',
        ]
        objs = []
        for src in sources:
            obj = tmp / (src.stem + '.o')
            subprocess.run([args.cxx, *cflags, '-c', str(src), '-o', str(obj)], check=True)
            objs.append(obj)

        # Verify AppleARMBacklight's new globals live in DATA/BSS and don't invent extra __mod_init_func entries.
        nm_out = subprocess.run(['nm', '-n', str(tmp / 'AppleARMBacklight.o')], check=True, capture_output=True, text=True).stdout
        for sym in ('gSavedUserLevel', 'gDisplayPowered', 'gLastWakeAbs', 'gBacklightInstance'):
            assert sym in nm_out, f'missing expected POD symbol {sym} in AppleARMBacklight.o'
        mod_init_count = sum(1 for line in nm_out.splitlines() if '__GLOBAL__sub_I_' in line)
        assert mod_init_count <= 1, f'unexpected extra global constructors in AppleARMBacklight.o: {mod_init_count}'
        print('PASS [4/4]: Zero unexpected __mod_init_func constructors; all 5 real C++ units compiled with -Wall -Wextra -Werror')

        bin1 = tmp / 'test_pm_buttons'
        subprocess.run([args.cxx, *cflags, str(harness_cpp), *[str(o) for o in objs], '-o', str(bin1)], check=True)
        subprocess.run([str(bin1)], check=True)

        bin2 = tmp / 'test_extra'
        subprocess.run([args.cxx, *cflags, str(extra_cpp), str(tmp / 'AstonPanel.o'), '-o', str(bin2)], check=True)
        subprocess.run([str(bin2)], check=True)


if __name__ == '__main__':
    main()

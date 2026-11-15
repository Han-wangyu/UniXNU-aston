// SPDX-License-Identifier: BSD-2-Clause-Patent
// Default-off original HID observation; a separate explicit gate starts an
// owned offline input source. No entitlement or original provider is replaced.
#include <IOKit/IOService.h>
#include <IOKit/IOLib.h>
#include <libkern/c++/OSMetaClass.h>
#include <libkern/c++/OSSymbol.h>
#include <libkern/c++/OSIterator.h>
#include <libkern/c++/OSNumber.h>
#include <libkern/c++/OSKext.h>
#include <kern/thread.h>
#include <pexpert/pexpert.h>
#include <mach/kmod.h>
#include <mach/mach_time.h>
extern kmod_info_t g_kernel_kmod_info;
extern "C" void native_hid_static_init(void) __asm__("__GLOBAL__sub_I_NativeHidInput.cpp");
extern "C" IOService *native_hid_input_allocate(void);
extern "C" IOService *native_hid_touch_allocate(void);
extern "C" {
extern kern_return_t OSKextLoadKextWithIdentifier(const char *);
volatile uint32_t native_hid_stage,native_hid_resource_found;
volatile int native_hid_load_status=-1;
volatile int native_hid_matching_load_status=-1;
volatile uint32_t native_hid_class_sizes[5];
volatile int native_hid_input_meta_status=-1;
volatile uint64_t native_hid_input_object;
volatile uint32_t native_hid_input_events[8];
volatile int native_hid_input_results[8]={-1,-1,-1,-1,-1,-1,-1,-1};
volatile uint32_t native_hid_input_command;
char native_hid_input_registry[2048];
char native_hid_input_props[1024];
extern volatile uint32_t native_legacy_transfer_calls;
// Optional unattended Home presses for hardware, which has no external harness writing the mailbox:
// offline-hid-auto-home=<seconds after the second presented frame>, offline-hid-auto-home-count (default 1)
// and offline-hid-auto-home-interval (default 20 s). Disabled unless the first argument is set.
static unsigned autoConfigured,autoFirst,autoCount,autoInterval,autoSent;
static uint64_t autoNext,autoFrequency;
static uint64_t counterNow(void){uint64_t v;__asm__ volatile("isb\n\tmrs %0, cntpct_el0":"=r"(v));return v;}
bool native_hid_input_requested(void) {
    if(native_legacy_transfer_calls<2)return false;
    if(!autoConfigured) {
        autoConfigured=1;autoFirst=0;autoCount=1;autoInterval=20;
        PE_parse_boot_argn("offline-hid-auto-home",&autoFirst,sizeof(autoFirst));
        PE_parse_boot_argn("offline-hid-auto-home-count",&autoCount,sizeof(autoCount));
        PE_parse_boot_argn("offline-hid-auto-home-interval",&autoInterval,sizeof(autoInterval));
        if(autoCount>8)autoCount=8;if(autoInterval<5)autoInterval=5;
        __asm__ volatile("mrs %0, cntfrq_el0":"=r"(autoFrequency));
        if(autoFirst && autoFrequency)autoNext=counterNow()+(uint64_t)autoFirst*autoFrequency;
    }
    if(autoFirst && autoFrequency && autoSent<autoCount && counterNow()>=autoNext) {
        ++autoSent;autoNext+=(uint64_t)autoInterval*autoFrequency;return true;
    }
    if(native_hid_input_command!=1)return false;
    native_hid_input_command=0;return true;
}
static void describeInput(IORegistryEntry *entry,unsigned depth,size_t &used) {
    if(depth>4 || used>=sizeof(native_hid_input_registry)-128)return;
    int count=snprintf(native_hid_input_registry+used,sizeof(native_hid_input_registry)-used,
        "%u %s [%s]\n",depth,entry->getName(),entry->getMetaClass()->getClassName());
    if(count<=0 || (size_t)count>=sizeof(native_hid_input_registry)-used)return;
    used+=count;OSIterator *it=entry->getChildIterator(gIOServicePlane);
    if(!it)return;
    while(OSObject *child=it->getNextObject()) {
        IORegistryEntry *next=OSDynamicCast(IORegistryEntry,child);
        if(next)describeInput(next,depth+1,used);
    }
    it->release();
}
// Compact one property of the first IOHIDEventDriver below entry: XML tags dropped, key end -> '=', value end -> ' '.
static IORegistryEntry *findEventDriver(IORegistryEntry *parent,unsigned depth) {
    if(!depth)return nullptr;
    OSIterator *it=parent->getChildIterator(gIOServicePlane);if(!it)return nullptr;
    IORegistryEntry *found=nullptr;
    while(OSObject *child=it->getNextObject()) {
        IORegistryEntry *entry=OSDynamicCast(IORegistryEntry,child);
        if(!entry)continue;
        if(entry->metaCast("IOHIDEventDriver")){found=entry;break;}
        if((found=findEventDriver(entry,depth-1)))break;
    }
    it->release();return found;
}
static void appendProperty(IORegistryEntry *driver,const char *key,const char *label,size_t &used) {
    OSObject *value=driver->copyProperty(key);
    int n=snprintf(native_hid_input_props+used,sizeof(native_hid_input_props)-used,"%s:",label);
    if(n>0)used+=n;
    if(value) {
        OSSerialize *text=OSSerialize::withCapacity(4096);
        if(text && value->serialize(text)) {
            const char *in=text->text();bool tag=false;char name[16];unsigned nameLen=0;
            for(unsigned i=0;in && in[i] && used<sizeof(native_hid_input_props)-2;++i) {
                char c=in[i];
                if(c=='<'){tag=true;nameLen=0;continue;}
                if(tag) {
                    if(c=='>') {
                        tag=false;name[nameLen]=0;
                        if(!strcmp(name,"/key"))native_hid_input_props[used++]='=';
                        else if(name[0]=='/')native_hid_input_props[used++]=' ';
                        else if(!strcmp(name,"true/")||!strcmp(name,"false/")){
                            used+=snprintf(native_hid_input_props+used,sizeof(native_hid_input_props)-used,"%s ",name[0]=='t'?"T":"F");
                        }
                    } else if(nameLen<sizeof(name)-1)name[nameLen++]=c;
                    continue;
                }
                if(c=='\n'||c=='\t')continue;
                native_hid_input_props[used++]=c;
            }
        }
        OSSafeReleaseNULL(text);OSSafeReleaseNULL(value);
    } else if(used<sizeof(native_hid_input_props)-6) {memcpy(native_hid_input_props+used,"none ",5);used+=5;}
    native_hid_input_props[used]=0;
}
static bool hasUsagePair(IORegistryEntry *driver,uint32_t expectedPage,uint32_t expectedUsage) {
    OSObject *pageObj=driver->copyProperty("PrimaryUsagePage");
    OSObject *usageObj=driver->copyProperty("PrimaryUsage");
    OSNumber *page=OSDynamicCast(OSNumber,pageObj);
    OSNumber *usage=OSDynamicCast(OSNumber,usageObj);
    const bool ok=page && usage && page->unsigned32BitValue()==expectedPage && usage->unsigned32BitValue()==expectedUsage;
    OSSafeReleaseNULL(pageObj);
    OSSafeReleaseNULL(usageObj);
    return ok;
}
__attribute__((unused)) static bool hasDigitizerPair(IORegistryEntry *driver) {
    return hasUsagePair(driver,0x0du,4u);
}
static bool hasPublishedProps(IORegistryEntry *driver,uint32_t expectedPage,uint32_t expectedUsage) {
    OSObject *builtInObj=driver->copyProperty("Built-In");
    const bool builtIn=(builtInObj==kOSBooleanTrue);
    OSSafeReleaseNULL(builtInObj);
    return builtIn && hasUsagePair(driver,expectedPage,expectedUsage);
}
static bool isDigitizerDevice(IORegistryEntry *device) {
    return device && device->getProperty("DigitizerCollectionDispatch")!=nullptr;
}
static bool isButtonsDevice(IORegistryEntry *device) {
    return device && device->getProperty("ConsumerButtonsOnly")!=nullptr && !isDigitizerDevice(device);
}
static void publishServiceProps(IORegistryEntry *driver,uint32_t page,uint32_t usage,bool displayIntegrated) {
    driver->setProperty("Built-In",kOSBooleanTrue);
    if(displayIntegrated)driver->setProperty("DisplayIntegrated",kOSBooleanTrue);
    // backboardd (-[BKHIDDirectTouchEventProcessor processEvent:sender:dispatcher:]) drops a digitizer event unless the sending
    // service has PrimaryUsagePage 0x0D and PrimaryUsage 1, 4 or 0x20, while ButtonResolver matches {PrimaryUsagePage: 12,
    // PrimaryUsage: 1, Built-In: true}. Publishing on both the event service and its IOHIDInterface parent closes the DriverKit
    // startup overwrite window before backboardd inspects the service.
    driver->setProperty("PrimaryUsagePage",(unsigned long long)page,32);
    driver->setProperty("PrimaryUsage",(unsigned long long)usage,32);
    if(IORegistryEntry *interface=driver->getParentEntry(gIOServicePlane)) {
        interface->setProperty("Built-In",kOSBooleanTrue);
        if(displayIntegrated)interface->setProperty("DisplayIntegrated",kOSBooleanTrue);
        interface->setProperty("PrimaryUsagePage",(unsigned long long)page,32);
        interface->setProperty("PrimaryUsage",(unsigned long long)usage,32);
    }
}
// backboardd caches the pair when it first sees the service, about 110 ms after the DriverKit driver registers it. This
// thread checks every 2 ms until the service has been registered for 10 s, at most 180 s.
static void servicePairGuard(void *context,wait_result_t) {
    IORegistryEntry *device=static_cast<IORegistryEntry *>(context);
    const bool isDigitizer=isDigitizerDevice(device);
    const bool isButtons=isButtonsDevice(device);
    if(!isDigitizer && !isButtons)return;
    const uint32_t page=isDigitizer?0x0du:0x0cu;
    const uint32_t usage=isDigitizer?4u:1u;
    unsigned stopAt=180000;
    for(unsigned ms=0;ms<stopAt;ms+=2) {
        if(IORegistryEntry *driver=findEventDriver(device,4)) {
            if(!hasPublishedProps(driver,page,usage)) {
                publishServiceProps(driver,page,usage,isDigitizer);
                IOLog("[hid] %s usage pair (0x%x,%u) published at +%u ms\n",isDigitizer?"digitizer":"buttons",page,usage,ms);
            }
            IOService *service=OSDynamicCast(IOService,driver);
            if(stopAt==180000 && service && (service->getState()&kIOServiceRegisteredState) && ms+10000<stopAt)stopAt=ms+10000;
        }
        IOSleep(2);
    }
}
__attribute__((noinline)) void native_hid_live_checkpoint(IOService *device) {
    size_t used=0;native_hid_input_registry[0]=0;describeInput(device,0,used);
    if(IORegistryEntry *driver=findEventDriver(device,4)) {
        size_t props=0;native_hid_input_props[0]=0;
        // Set once per device only: repeating setProperty each second keeps backboardd/hidd busy re-evaluating the service.
        // Keep separate static pointers for digitizer vs consumer buttons so two active NativeHidInput instances never ping-pong.
        if(isDigitizerDevice(device)) {
            static IORegistryEntry *publishedDigitizerFor;
            if(publishedDigitizerFor!=driver || !hasPublishedProps(driver,0x0d,4)) {
                publishedDigitizerFor=driver;
                publishServiceProps(driver,0x0d,4,true);
            }
        } else if(isButtonsDevice(device)) {
            static IORegistryEntry *publishedButtonsFor;
            if(publishedButtonsFor!=driver || !hasPublishedProps(driver,0x0c,1)) {
                publishedButtonsFor=driver;
                publishServiceProps(driver,0x0c,1,false);
            }
        }
        // The Digitizer/DebugState dumps (appendProperty) are not read on hardware runs without a photograph and walk
        // driver-internal state from a foreign thread; only the cheap Built-In readback stays.
        appendProperty(driver,"Built-In","BI",props);
        appendProperty(driver,"PrimaryUsagePage","PP",props);appendProperty(driver,"PrimaryUsage","PU",props);
    }
    __asm__ volatile("" ::: "memory");
}
// The time (mach units) of every touch report handed to the HID event driver (event 6), the newest 65536: the touch windows the
// latency analysis is limited to. Allocated by BootDiagExport.c once the home screen draws (a static array of this size does not fit
// the kernel's data segment).
volatile uint64_t *native_touch_report_ring;
volatile uint32_t native_touch_report_count;
void native_hid_input_observed(unsigned event,int result) {
    if(event<8){native_hid_input_events[event]=native_hid_input_events[event]+1;native_hid_input_results[event]=result;}
    if(event==6 && native_touch_report_ring) {
        const uint32_t n=native_touch_report_count;
        native_touch_report_ring[n&65535]=mach_absolute_time();
        native_touch_report_count=n+1;
    }
}
__attribute__((noinline)) void native_hid_checkpoint(void) { __asm__ volatile("" ::: "memory"); }
void native_hid_input_probe(void) {
    unsigned enabled=0;
    if(!PE_parse_boot_argn("offline-hid-input",&enabled,sizeof(enabled)) || enabled!=1)return;
    if(native_hid_stage)return;
    native_hid_stage=1;
    native_hid_load_status=OSKextLoadKextWithIdentifier("com.apple.iokit.IOHIDFamily");
    if(!native_hid_load_status)
        native_hid_matching_load_status=OSKext::loadKextWithIdentifier("com.apple.iokit.IOHIDEventDriver",
            true,false,kOSKextExcludeNone,kOSKextExcludeNone,nullptr);
    native_hid_stage=2;
    const char *names[]={"IOHIDUserDevice","IOHIDEventDriver","IOHIDEventService","IOHIDResourceDeviceUserClient","IOHIDDevice"};
    for(unsigned i=0;i<5;++i) {
        const OSSymbol *name=OSSymbol::withCString(names[i]);
        const OSMetaClass *meta=name?OSMetaClass::getMetaClassWithName(name):nullptr;
        if(meta)native_hid_class_sizes[i]=meta->getClassSize();
        OSSafeReleaseNULL(name);
    }
    if(native_hid_load_status==0) {
        OSDictionary *match=IOService::serviceMatching("IOHIDResource");
        IOService *resource=match?IOService::waitForMatchingService(match,5000000000ULL):nullptr;
        native_hid_resource_found=resource!=nullptr;
        unsigned sendHome=0;PE_parse_boot_argn("offline-hid-home",&sendHome,sizeof(sendHome));
        if(resource && sendHome==1 && native_hid_class_sizes[4]==208) {
            void *handle=OSMetaClass::preModLoad(g_kernel_kmod_info.name);
            if(handle) {
                native_hid_static_init();native_hid_input_meta_status=OSMetaClass::postModLoad(handle);
                if(!native_hid_input_meta_status) {
                    IOService *device=native_hid_input_allocate();
                    native_hid_input_object=(uintptr_t)device;
                    bool valid=device && device->metaCast("IOHIDDevice") && device->init();
                    native_hid_input_observed(0,valid?0:kIOReturnError);
                    bool attached=valid && device->attach(resource);
                    native_hid_input_observed(1,attached?0:kIOReturnError);
                    bool started=attached && device->start(resource);
                    if(!started) {
                        native_hid_input_observed(7,kIOReturnError);
                        if(attached)device->detach(resource);
                        OSSafeReleaseNULL(device);native_hid_input_object=0;
                    }
                    unsigned split=0;
                    PE_parse_boot_argn("sheng-touch-split",&split,sizeof(split));
                    if(started && (isDigitizerDevice(device) || isButtonsDevice(device))) {
                        thread_t guard=nullptr;
                        if(kernel_thread_start(servicePairGuard,device,&guard)==KERN_SUCCESS)thread_deallocate(guard);
                    }
                    unsigned touchHid=0;
                    if(started && split==1 && PE_parse_boot_argn("sheng-touch-hid",&touchHid,sizeof(touchHid)) && touchHid==1) {
                        // Separate digitizer service (primary usage Digitizer/Touch Screen, Built-In).
                        IOService *touch=native_hid_touch_allocate();
                        bool touchStarted=touch && touch->metaCast("IOHIDDevice") && touch->init() && touch->attach(resource) && touch->start(resource);
                        if(touchStarted) {
                            thread_t guard=nullptr;
                            if(kernel_thread_start(servicePairGuard,touch,&guard)==KERN_SUCCESS)thread_deallocate(guard);
                        }
                        if(!touchStarted && touch){touch->detach(resource);OSSafeReleaseNULL(touch);}
                    }
                    // The offline devices live for this bounded boot. Its
                    // service attachment and timer remain genuine live objects.
                }
            }
        }
        OSSafeReleaseNULL(resource);OSSafeReleaseNULL(match);
    }
    native_hid_stage=3;native_hid_checkpoint();
}
}


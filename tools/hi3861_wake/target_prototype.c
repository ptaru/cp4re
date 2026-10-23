/* CP4 2.139 independent UDP wake with OEM prewake. See docs/HI3861-LAN-PATCH.md.
 * Replaces OEM UDP receiver; not a parallel second service. */
#include "receiver_core.h"
#define CALL(addr, ret, args) ((ret (*) args)(uintptr_t)(addr))
struct addr4 { uint16_t family, port; uint32_t ip; unsigned char zero[8]; };
struct tv { int32_t sec, usec; };
struct runtime { int fd; struct wake_status status; };
_Static_assert(sizeof(struct wake_status)==80,"status wire size");
_Static_assert(sizeof(struct addr4)==16,"sockaddr ABI");
_Static_assert(sizeof(struct tv)==8,"select timeval ABI");
static int open_socket(void *v) {
    struct runtime *r=v;
    struct addr4 a={2,(uint16_t)((WAKE_PORT>>8)|(WAKE_PORT<<8)),0,{0}};
    int fd=CALL(0x462b4c,int,(int,int,int))(2,2,0);
    /* select implementation rejects nfds outside 1..9. */
    if (fd<0) return fd;
    if(fd>8 || CALL(0x462398,int,(int,void*,int))(fd,&a,16)) {
        CALL(0x462488,int,(int))(fd);return -1;
    }
    r->fd=fd;return 0;
}
static void close_socket(void *v) { CALL(0x462488,int,(int))(((struct runtime *)v)->fd); }
static int ensure_filter(void *v) { (void)v; return CALL(0x41ce4c,int,(int))(WAKE_PORT); }
static int assert_wake(void *v) {
    (void)v;CALL(0x41e5e2,int,(unsigned))(20); /* OEM temporary Wi-Fi prewake */
    if(!*(volatile unsigned *)0xe2140) {
        *(volatile unsigned char *)0xe2138=0;
        if(*(volatile unsigned *)0xe2414) CALL(0x4a64bc,int,(int,int))(1,1000);
    }
    CALL(0x41d9de,void,(int))(4);
    CALL(0x41e74a,void,(void))();return 0; /* GPIO helper has no reliable status return */
}
__attribute__((section(".receiver_entry")))
void replacement_receiver(void *unused) {
    (void)unused;
    struct runtime r={0};struct wake_io io={open_socket,close_socket,ensure_filter,assert_wake,&r};
    uint32_t next_filter=0;int previous=-1;
    wake_init(&r.status);
    /* Publication uses an otherwise unused defense word; only valid in replacement mode.
     * Must be checked against every surviving writer before producing an OTA. */
    *(volatile uintptr_t *)0xe21cc=(uintptr_t)&r.status;
    for (;;) {
        uint32_t now=(uint32_t)CALL(0x4c6018,uint64_t,(void))();
        int online=CALL(0x421b68,int,(void))()==3;
        if(online!=previous) { wake_network_changed(&r.status,&io,online);previous=online; }
        if((int32_t)(now-next_filter)>=0) { r.status.filter_ready=0;next_filter=now+10000; }
        wake_service(&r.status,&io,now);
        if(!r.status.socket_ready || !r.status.filter_ready) {
            CALL(0x3f587c,int,(unsigned))(1000);continue;
        }
        uint32_t set[32]={0};struct tv timeout={0,250000};
        set[(unsigned)r.fd>>5]=1u<<((unsigned)r.fd&31);
        int n=CALL(0x462c70,int,(int,void*,void*,void*,void*))(r.fd+1,set,0,0,&timeout);
        if(n<0) { wake_socket_failed(&r.status,&io,now,n);continue; }
        if(n>0) {
            unsigned char packet[sizeof(wake_magic)+1];
            n=CALL(0x4626c8,int,(int,void*,int,int,void*,void*))(r.fd,packet,sizeof packet,0,0,0);
            if(n<0) { wake_socket_failed(&r.status,&io,now,n);continue; }
            now=(uint32_t)CALL(0x4c6018,uint64_t,(void))();
            wake_packet(&r.status,&io,now,packet,(unsigned)n);
        }
    }
}
/* Proposed replacement of jal at 0x41f0d4. Preserve the original return value. */
__attribute__((section(".bootstrap")))
int replacement_bootstrap(void) {
    int result=CALL(0x41e93a,int,(void))();
    for(unsigned attempt=0;attempt<3;attempt++) {
        CALL(0x420942,void,(void))();
        if(*(volatile unsigned *)0xe21dc) break;
        if(attempt<2) CALL(0x3f587c,int,(unsigned))(100);
    }
    return result;
}
/* New top-level HCC command 0x12, not an EZVI inner pattern subcommand.
 * Returns a versioned read-only snapshot, without draining or rearming anything. */
__attribute__((section(".status")))
void replacement_status_reply(void) {
    unsigned char reply[1+sizeof(struct wake_status)];
    reply[0]=0x12;
    unsigned irq=CALL(0x3f50d2,unsigned,(void))();
    const struct wake_status *s=*(const struct wake_status * volatile *)0xe21cc;
    if((uintptr_t)s>=0xdc000 && (uintptr_t)s<=0x120000-sizeof *s && !((uintptr_t)s&3)) CALL(0x3fb01a,void*,(void*,const void*,unsigned))(reply+1,s,sizeof *s);
    else CALL(0x3fb2fa,void*,(void*,int,unsigned))(reply+1,0,sizeof(struct wake_status));
    CALL(0x3f50de,void,(unsigned))(irq);
    CALL(0x4c6510,int,(void*,unsigned))(reply,sizeof reply);
}

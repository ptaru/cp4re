/* Host-side checks for the real reader's framing and circular-history code. */
#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif
#define main server_main
#include "cp4-video.c"
#undef main
#include <assert.h>

int main(void) {
    Client c={0};c.queue=malloc(QUEUE);
    unsigned char *arena=calloc(1,RING_SIZE+8);
    unsigned char pack[100];memset(pack,0x55,sizeof pack);
    memcpy(pack,"\0\0\1\xba",4);memcpy(pack+20,"\0\0\1\xbb",4);
    memcpy(pack+44,"\0\0\1\xbc",4);
    const unsigned char junk[]={9,0,0,1,0xba,4,5};
    assert(!stream_data(&c,junk,sizeof junk));assert(!c.aligned);
    for(size_t i=0;i<sizeof pack;i++)assert(!stream_data(&c,pack+i,1));
    assert(c.aligned&&c.len==100&&!memcmp(c.queue,pack,100));
    c.sent=98;assert(!enqueue(&c,"ab",2));
    assert(c.len==4&&!memcmp(c.queue,"UUab",4));
    assert(enqueue(&c,"x",QUEUE)==-1);
    /* Place a header-bearing pack across the end of the circular ring. */
    for(size_t i=0;i<sizeof pack;i++)arena[8+((RING_SIZE-40+i)&(RING_SIZE-1))]=pack[i];
    c.len=c.sent=c.aligned=c.primed=0;
    prime(&c,arena,60);
    assert(c.primed&&c.aligned&&c.len==100&&!memcmp(c.queue,pack,100));
    /* A later header wins, so joining does not replay an older GOP. */
    memcpy(arena+8+60,pack,100);
    c.len=c.sent=c.aligned=c.primed=0;prime(&c,arena,160);
    assert(c.len==100&&!memcmp(c.queue,pack,100));
    free(arena);free(c.queue);puts("PASS: split pack start, queue compaction/limit, wrapped history, latest header");
}

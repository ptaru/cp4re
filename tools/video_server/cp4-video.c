#define _GNU_SOURCE
#define _FILE_OFFSET_BITS 64
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/shm.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <dirent.h>

/* Exact CP4 V5.3.3 build 251118 layout; never write media/control segments. */
#define ARENA_KEY 0x650100c1
#define CONTROL_KEY 0x640100c1
#define RING_SIZE 0x400000u
#define DESC 0x8c370
#define SLEEP_ADDR 6753236
#define CLIENTS 8
#define QUEUE (1024*1024)
static volatile sig_atomic_t stopping;
static void stop(int sig) { (void)sig; stopping = 1; }
static uint64_t now_ms(void) {
    struct timespec t; clock_gettime(CLOCK_MONOTONIC, &t);
    return (uint64_t)t.tv_sec*1000+t.tv_nsec/1000000;
}
static int find_app(void) {
    DIR *d=opendir("/proc"); struct dirent *e; int pid=-1;
    if (!d) return -1;
    while ((e=readdir(d))) {
        if (e->d_name[0]<'0'||e->d_name[0]>'9') continue;
        char path[128], cmd[128]={0};
        snprintf(path,sizeof path,"/proc/%s/cmdline",e->d_name);
        int f=open(path,O_RDONLY); if(f<0) continue;
        ssize_t n=read(f,cmd,sizeof(cmd)-1); close(f);
        if(n>0 && !strcmp(cmd,"/dav/ezapp")) {pid=atoi(e->d_name); break;}
    }
    closedir(d); return pid;
}
/* The guardian owns the only writable process-memory FD. This service owns
 * the sleep hold exclusively: release/EOF restores sleep permission to 1. */
static int guardian(int *command, int *reply) {
    int p[2], a[2]; if(pipe(p)||pipe(a)) return -1;
    pid_t child=fork(); if(child<0) return -1;
    if(child) {close(p[0]);close(a[1]);*command=p[1];*reply=a[0];return child;}
    close(p[1]);close(a[0]);
    signal(SIGINT,SIG_IGN);signal(SIGTERM,SIG_IGN);signal(SIGHUP,SIG_IGN);
    int mem=-1; uint32_t saved=1; int held=0;
    for(;;) {
        struct pollfd f={p[0],POLLIN,0}; int rc=poll(&f,1,2000);
        if(rc<0 && errno==EINTR) continue;
        if(rc==0) {
            uint32_t zero=0;
            if(held && pwrite(mem,&zero,4,SLEEP_ADDR)!=4) break;
            continue;
        }
        char cmd; if(read(p[0],&cmd,1)!=1) break;
        char ok=1;
        if(cmd=='H'&&!held) {
            int pid=find_app(); char path[128], hash[80]={0};
            snprintf(path,sizeof path,"md5sum /proc/%d/exe",pid);
            FILE *fp=pid>0?popen(path,"r"):NULL;
            if(fp) {if(!fgets(hash,sizeof hash,fp)) hash[0]=0; pclose(fp);}
            if(strncmp(hash,"8a4f3c1bf4f850318a17fae01305c20b",32)) ok=0;
            snprintf(path,sizeof path,"/proc/%d/mem",pid);
            if(ok) mem=open(path,O_RDWR);
            if(mem<0) ok=0;
            uint32_t observed;
            if(ok && (pread(mem,&observed,4,SLEEP_ADDR)!=4 || observed>1)) ok=0;
            uint32_t zero=0;
            if(ok) {held=1;if(pwrite(mem,&zero,4,SLEEP_ADDR)!=4) ok=0;}
            if(!ok) {
                if(held) pwrite(mem,&saved,4,SLEEP_ADDR);
                held=0; if(mem>=0)close(mem);mem=-1;
            }
        } else if(cmd=='R'&&held) {
            if(pwrite(mem,&saved,4,SLEEP_ADDR)!=4) ok=0;
            if(ok) {held=0;close(mem);mem=-1;}
        }
        if(write(a[1],&ok,1)!=1) break;
    }
    if(held) pwrite(mem,&saved,4,SLEEP_ADDR);
    _exit(0);
}
static int hold_command(int command,int reply,char c) {
    char ok=0; return write(command,&c,1)==1 && read(reply,&ok,1)==1 && ok;
}
typedef struct {
    int fd, stream, aligned, primed;
    size_t len, sent, request_len;
    char request[2048];
    unsigned char *queue;
    uint64_t since;
} Client;
static void disconnect(Client *c) {
    close(c->fd); free(c->queue); memset(c,0,sizeof *c);c->fd=-1;
}
static int enqueue(Client *c,const void *data,size_t n) {
    if(c->sent) {memmove(c->queue,c->queue+c->sent,c->len-c->sent);c->len-=c->sent;c->sent=0;}
    if(n>QUEUE-c->len) return -1;
    memcpy(c->queue+c->len,data,n);c->len+=n;return 0;
}
static int stream_data(Client *c,const unsigned char *data,size_t n) {
    if(c->aligned) return enqueue(c,data,n);
    /* Wait for a complete header-bearing pack if history had no usable GOP.
     * This firmware places system header at +20 and program map at +44. */
    for(size_t i=0;i<n;i++) {
        if(c->request_len==48) {memmove(c->request,c->request+1,47);c->request_len=47;}
        c->request[c->request_len++]=(char)data[i];
        if(c->request_len==48 && !memcmp(c->request,"\0\0\1\xba",4) &&
           !memcmp(c->request+20,"\0\0\1\xbb",4) &&
           !memcmp(c->request+44,"\0\0\1\xbc",4)) {
            c->aligned=1;
            if(enqueue(c,c->request,48)) return -1;
            return enqueue(c,data+i+1,n-i-1);
        }
    }
    return 0;
}
static void *attach(int key,size_t minimum) {
    int id=shmget(key,1,0); struct shmid_ds info;
    if(id<0 || shmctl(id,IPC_STAT,&info) || info.shm_segsz<minimum) return NULL;
    void *p=shmat(id,NULL,SHM_RDONLY); return p==(void*)-1?NULL:p;
}
static uint32_t cursor(const unsigned char *control) {
    uint32_t w=*(volatile const uint32_t *)(control+DESC+8);
    __atomic_thread_fence(__ATOMIC_ACQUIRE);
    return w;
}
/* A bounded history snapshot lets a join start at the most recent PS system
 * header / program map / IDR rather than waiting with undecodable P frames.
 * Copy only bytes behind our current cursor; never move a producer cursor. */
static void prime(Client *c,const unsigned char *arena,uint32_t end) {
    const size_t size=768*1024;
    unsigned char *history=malloc(size);
    c->primed=1;
    if(!history)return;
    uint32_t start=(end-size)&(RING_SIZE-1);
    size_t first=RING_SIZE-start;if(first>size)first=size;
    memcpy(history,arena+8+start,first);
    memcpy(history+first,arena+8,size-first);
    size_t chosen=size;
    for(size_t i=0;i+48<size;i++) {
        if(!memcmp(history+i,"\0\0\1\xba",4) &&
           !memcmp(history+i+20,"\0\0\1\xbb",4) &&
           !memcmp(history+i+44,"\0\0\1\xbc",4)) chosen=i;
    }
    if(chosen<size && !enqueue(c,history+chosen,size-chosen))c->aligned=1;
    free(history);
}
int main(int argc,char **argv) {
    int port=argc>1?atoi(argv[1]):8080;
    const char *bind_ip=argc>2?argv[2]:"0.0.0.0";
    if(port<1||port>65535) return 2;
    signal(SIGPIPE,SIG_IGN);signal(SIGINT,stop);signal(SIGTERM,stop);signal(SIGHUP,stop);
    int command,reply,guard=guardian(&command,&reply);if(guard<0)return 1;
    int listener=socket(AF_INET,SOCK_STREAM,0),one=1;
    struct sockaddr_in addr={.sin_family=AF_INET,.sin_port=htons(port)};
    if(inet_pton(AF_INET,bind_ip,&addr.sin_addr)!=1) {close(command);return 2;}
    setsockopt(listener,SOL_SOCKET,SO_REUSEADDR,&one,sizeof one);
    if(bind(listener,(void*)&addr,sizeof addr)||listen(listener,8)) {perror("listen");close(command);return 1;}
    fcntl(listener,F_SETFL,O_NONBLOCK);
    Client clients[CLIENTS];memset(clients,0,sizeof clients);
    for(int i=0;i<CLIENTS;i++)clients[i].fd=-1;
    unsigned char *arena=NULL,*control=NULL, data[65536];
    uint32_t read_at=0;int positioned=0,held=0,ever_connected=0;uint64_t last_read=0,last_attach=0;
    /* Brief startup grace gives the first viewer time to connect after a
     * manual launch. After that, only actual streaming clients hold sleep. */
    uint64_t startup_until=now_ms()+10000;
    held=hold_command(command,reply,'H');
    if(!held) {
        fprintf(stderr,"cannot acquire verified sleep hold; refusing startup\n");
        close(listener);close(command);close(reply);waitpid(guard,NULL,0);return 1;
    }
    fprintf(stderr,"cp4-video listening %s:%d GET /live.ps (read-only ring 0)\n",bind_ip,port);
    while(!stopping) {
        if(waitpid(guard,NULL,WNOHANG)==guard) {
            fprintf(stderr,"sleep guardian exited; closing streams\n");break;
        }
        struct pollfd f[CLIENTS+1];f[0]=(struct pollfd){listener,POLLIN,0};
        for(int i=0;i<CLIENTS;i++) f[i+1]=(struct pollfd){clients[i].fd,
            POLLIN|(clients[i].len>clients[i].sent?POLLOUT:0),0};
        if(poll(f,CLIENTS+1,20)<0 && errno!=EINTR) break;
        uint64_t now=now_ms();
        if(f[0].revents&POLLIN) {
            int fd=accept(listener,NULL,NULL),slot=-1;
            for(int i=0;i<CLIENTS;i++) if(clients[i].fd<0) {slot=i;break;}
            if(fd>=0) {
                if(slot<0)close(fd);
                else {
                    fcntl(fd,F_SETFL,O_NONBLOCK);
                    clients[slot].fd=fd;clients[slot].since=now;
                    clients[slot].queue=malloc(QUEUE);
                    if(!clients[slot].queue)disconnect(&clients[slot]);
                }
            }
        }
        for(int i=0;i<CLIENTS;i++) {
            Client *c=&clients[i]; if(c->fd<0)continue;
            short re=f[i+1].revents;
            if(re&(POLLERR|POLLHUP|POLLNVAL)) {disconnect(c);continue;}
            if(re&POLLIN) {
                char buf[1024];ssize_t n=recv(c->fd,buf,sizeof buf,0);
                if(n==0 || (n<0 && errno!=EAGAIN && errno!=EINTR)) {disconnect(c);continue;}
                if(n>0&&!c->stream) {
                    if((size_t)n>=sizeof(c->request)-c->request_len) {disconnect(c);continue;}
                    memcpy(c->request+c->request_len,buf,n);c->request_len+=n;c->request[c->request_len]=0;
                    if(strstr(c->request,"\r\n\r\n")) {
                        if(strncmp(c->request,"GET /live.ps HTTP/1.",19)) {disconnect(c);continue;}
                        if(!held) {
                            if(!hold_command(command,reply,'H')) {fprintf(stderr,"sleep hold failed; refusing stream\n");disconnect(c);continue;}
                            held=1;fprintf(stderr,"sleep hold acquired\n");
                        }
                        c->stream=1;c->request_len=0;
                        ever_connected=1;
                        const char *h="HTTP/1.0 200 OK\r\nContent-Type: video/mpeg\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n";
                        enqueue(c,h,strlen(h));
                    }
                }
            }
            if(c->len>c->sent) {
                ssize_t n=send(c->fd,c->queue+c->sent,c->len-c->sent,MSG_NOSIGNAL);
                if(n>0) {c->sent+=n;c->since=now;}
                else if(n<0&&errno!=EAGAIN&&errno!=EINTR) {disconnect(c);continue;}
            }
            if((!c->stream||c->len>c->sent)&&now-c->since>5000)disconnect(c);
        }
        int active=0;for(int i=0;i<CLIENTS;i++)active+=clients[i].fd>=0&&clients[i].stream;
        if(!active) {
            positioned=0;
            if(held) {
                if(!ever_connected&&now<startup_until)continue;
                if(!hold_command(command,reply,'R')) {fprintf(stderr,"sleep restore failed\n");break;}
                held=0;fprintf(stderr,"sleep hold released\n");
            }
            if(arena) {shmdt(arena);arena=NULL;} if(control) {shmdt(control);control=NULL;}
            continue;
        }
        if(!arena||!control) {
            if(now-last_attach<250)continue;last_attach=now;
            if(!arena)arena=attach(ARENA_KEY,8+RING_SIZE);
            if(!control)control=attach(CONTROL_KEY,DESC+20);
            if(!arena||!control)continue;
            fprintf(stderr,"attached media/control read-only\n");
        }
        uint32_t w=cursor(control);
        if(w>=RING_SIZE) {fprintf(stderr,"invalid cursor %x\n",w);break;}
        if(!positioned) {read_at=w;positioned=1;last_read=now;}
        for(int i=0;i<CLIENTS;i++)if(clients[i].fd>=0&&clients[i].stream&&!clients[i].primed)
            prime(&clients[i],arena,read_at);
        /* A scheduling gap can hide a full lap. Drop clients rather than splice
         * old/new data. Normal polling is 20 ms; observed ring slack >30 s. */
        if(now-last_read>2000) {
            for(int i=0;i<CLIENTS;i++)if(clients[i].fd>=0)disconnect(&clients[i]);
            positioned=0;continue;
        }
        last_read=now;
        size_t available=(w-read_at)&(RING_SIZE-1);
        if(w<read_at)fprintf(stderr,"ring wrap %x -> %x\n",read_at,w);
        while(available) {
            size_t n=available;if(n>sizeof data)n=sizeof data;
            if(n>RING_SIZE-read_at)n=RING_SIZE-read_at;
            memcpy(data,arena+8+read_at,n);
            for(int i=0;i<CLIENTS;i++)if(clients[i].fd>=0&&clients[i].stream)
                if(stream_data(&clients[i],data,n))disconnect(&clients[i]);
            read_at=(read_at+n)&(RING_SIZE-1);available-=n;
        }
    }
    for(int i=0;i<CLIENTS;i++)if(clients[i].fd>=0)disconnect(&clients[i]);
    close(listener);if(arena)shmdt(arena);if(control)shmdt(control);
    close(command);close(reply);waitpid(guard,NULL,0);return 0;
}

#include "receiver_core.h"
/* No packet queue, allocation, printing, network reply or direct GPIO writes. */
void wake_init(struct wake_status *s) {
    *s = (struct wake_status){.magic=0x31574c43u, .version=1};
}
void wake_network_changed(struct wake_status *s, const struct wake_io *io, int online) {
    if (s->socket_ready) io->close_socket(io->ctx);
    s->socket_ready=0; s->filter_ready=0; s->online=!!online;
    s->retry_pending=0; s->generation++;
    /* Do not clear cooldown: association flapping must not bypass it. */
}
void wake_service(struct wake_status *s, const struct wake_io *io, uint32_t now) {
    if (!s->online) return;
    if (s->retry_pending && (int32_t)(now-s->retry_at_ms)<0) return;
    s->retry_pending=0;
    if (!s->socket_ready) {
        int r=io->open_bound_socket(io->ctx); s->opens++; s->last_socket_result=r;
        if (r<0) { s->socket_errors++; goto retry; }
        s->socket_ready=1;
    }
    if (!s->filter_ready) {
        int r=io->ensure_filter(io->ctx); s->last_filter_result=r;
        if (r) { s->filter_errors++; goto retry; }
        s->filter_ready=1;
    }
    return;
retry:
    s->retry_pending=1; s->retry_at_ms=now+WAKE_RETRY_MS;
}
void wake_socket_failed(struct wake_status *s, const struct wake_io *io, uint32_t now, int error) {
    if (s->socket_ready) io->close_socket(io->ctx);
    s->socket_ready=0; s->filter_ready=0; s->socket_errors++;
    s->last_socket_result=error; s->retry_at_ms=now+WAKE_RETRY_MS; s->retry_pending=1;
}
void wake_packet(struct wake_status *s, const struct wake_io *io, uint32_t now,
                 const unsigned char *p, size_t n) {
    unsigned i;
    s->received++; s->last_rx_ms=now;
    if (n!=sizeof(wake_magic)) { s->invalid++; return; }
    for (i=0; i<sizeof(wake_magic); i++) if(p[i]!=wake_magic[i]) { s->invalid++; return; }
    if (!s->online || !s->socket_ready || !s->filter_ready) { s->suppressed++; return; }
    if (s->have_asserted && (uint32_t)(now-s->last_assert_ms)<WAKE_COOLDOWN_MS) {
        s->suppressed++; return;
    }
    /* Existing host acknowledgement clears the level. Do not invent a pulse or
     * skip on a cached 'host awake' flag that could be stale. */
    if (!io->set_reason_and_assert(io->ctx)) {
        s->assertions++; s->last_assert_ms=now; s->have_asserted=1;
    }
}

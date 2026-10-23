/* Design prototype; no target addresses, startup hooks or flash image. */
#ifndef CP4_RECEIVER_CORE_H
#define CP4_RECEIVER_CORE_H
#include <stddef.h>
#include <stdint.h>
#define WAKE_PORT 50162
#define WAKE_COOLDOWN_MS 1000u
#define WAKE_RETRY_MS 1000u
#define WAKE_SERVICE_MS 250u
/* An identifier, not a secret or authentication credential. */
static const unsigned char wake_magic[12] = {'C','P','4','W','A','K','E',1,0x50,0x03,0xbd,0x2f};
struct wake_status {
    uint32_t magic, version, generation, online, socket_ready, filter_ready;
    uint32_t received, invalid, suppressed, assertions, opens, socket_errors;
    uint32_t filter_errors, last_rx_ms, last_assert_ms, retry_at_ms;
    int32_t last_socket_result, last_filter_result;
    uint32_t have_asserted, retry_pending;
};
/* One owning task. Event callbacks enqueue/set an epoch; they do not touch sockets.
 * The binding must read status under a snapshot lock/seqlock, not expose live RAM. */
struct wake_io {
    int (*open_bound_socket)(void *); /* IPv4 UDP, INADDR_ANY:WAKE_PORT */
    void (*close_socket)(void *);
    int (*ensure_filter)(void *); /* add/update exact MCU IPv4 UDP dst-port filter */
    int (*set_reason_and_assert)(void *); /* OEM reason 4, then OEM GPIO helper */
    void *ctx;
};
void wake_init(struct wake_status *);
void wake_network_changed(struct wake_status *, const struct wake_io *, int online);
void wake_service(struct wake_status *, const struct wake_io *, uint32_t now_ms);
void wake_socket_failed(struct wake_status *, const struct wake_io *, uint32_t now_ms, int error);
void wake_packet(struct wake_status *, const struct wake_io *, uint32_t now_ms,
                 const unsigned char *packet, size_t size);
#endif

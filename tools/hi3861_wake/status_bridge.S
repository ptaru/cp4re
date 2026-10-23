/* A jump-table entry inside ez_hichannel_task, not a normal function call. */
.section .status_bridge,"ax",@progbits
.global replacement_status_bridge
.type replacement_status_bridge,@function
replacement_status_bridge:
    call replacement_status_reply
    li t0, 0x41de90
    jr t0
.size replacement_status_bridge, .-replacement_status_bridge

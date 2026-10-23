/* Fixed reclaimed code regions for the CP4 2.139 receiver. */
OUTPUT_ARCH(riscv)
ENTRY(replacement_bootstrap)
MEMORY {
  former_defense (rx) : ORIGIN = 0x4209ba, LENGTH = 0x226
  former_receiver (rx) : ORIGIN = 0x420be0, LENGTH = 0x314
}
PROVIDE(memset = 0x3fb2fa);
SECTIONS {
  .bootstrap : {
    *(.bootstrap)
    *(.status)
    *(.status_bridge)
    *(.text.wake_init)
    *(.text.wake_network_changed)
    *(.text.wake_service)
    *(.text.wake_socket_failed)
  } > former_defense
  .receiver : {
    *(.receiver_entry)
    *(.text*)
    *(.rodata*)
  } > former_receiver
  /DISCARD/ : { *(.comment) *(.riscv.attributes) *(.eh_frame*) }
  ASSERT(replacement_receiver == 0x420be0, "OEM task entry must remain unchanged")
  ASSERT(replacement_bootstrap == 0x4209ba, "startup hook target must be exact")
}

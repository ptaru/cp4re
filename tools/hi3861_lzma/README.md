# LZMA helpers for the Hi3861L OTA

`tools/hi3861_ota.py patch` needs two small native helpers to rebuild the
compressed MCU image exactly as the CP4 OEM package does:

| File | Purpose |
| --- | --- |
| `encode.c` | File-to-file LZMA encoder using SDK 19.00 settings: dictionary 65,536; `lc=2`, `lp=2`, `pb=2`; normal algorithm; 128 fast bytes; one thread; known size and no end marker. |
| `decode.c` | Independent bounded-buffer decoder using the older SDK 9.20, with caller-selected input chunks. |

`hi3861_ota.py` compiles both with the system C compiler in a temporary
directory. Nothing is installed, and neither helper touches a camera or the
network.

## Why the native encoder

Python's `lzma` module (liblzma) does not reproduce the OEM stream. It writes
an unknown-size header and an end marker, and its match finder diverges from
this package. The SDK 19.00 C encoder, with the settings above, reproduces the
untouched 551,558-byte OEM stream **byte-for-byte**. The builder refuses to
proceed unless that control check passes.

The SDK 9.20 decoder is a separate compatibility check, not a model of the
on-chip decoder. It is fed the stream in 1, 13, 64 and 4,096-byte chunks.

## Upstream sources

The `sdk19/` and `sdk920/` directories contain an unmodified, minimal subset of
the official LZMA SDK archives:

- <https://www.7-zip.org/a/lzma1900.7z>
- <https://www.7-zip.org/a/lzma920.tar.bz2>

`PROVENANCE.json` lists the archive hashes and every copied file's source path
and SHA-256. The LZMA SDK is placed in the public domain by Igor Pavlov; those
files retain their upstream headers. The project's MIT license covers
`encode.c` and `decode.c`, not the SDK sources.

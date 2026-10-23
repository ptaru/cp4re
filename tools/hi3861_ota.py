#!/usr/bin/env python3
"""Validate, unpack, and rebuild the CP4 Hi3861L MCU OTA (offline).

The CP4 app0 filesystem contains two Hi3861L update images. This tool handles
the application OTA, `Hi3861L_ezviz_sdk_ota.bin`; it does not handle the
flashboot OTA, the camera `.dav` package, or installation.

The image is a HiSilicon update container:

    0x000  common header (0x60), key/signature area, 0x180 total
    0x180  256-byte SHA-256 slot for the common header
    0x280  section header (0x40)
    0x2c0  256-byte SHA-256 slot for the section
    0x3c0  LZMA-alone stream, then zero padding to a 16-byte boundary

The expanded section is another instance of the same wrapper, holding the
Hi3861L kernel. The patch replaces the UDP receiver and installs bounded
startup, sleep-filter and diagnostic hooks for CP4 version 2.139.

`patch` is deliberately pinned to the CP4 MCU application version 2.139
(source SHA-256 e01c4867...). It builds the same receiver image described in
docs/HI3861-LAN-PATCH.md, reproduces the OEM compressed stream byte-for-byte
as a control, and verifies the result independently with the SDK 9.20 decoder.
It writes an offline image only; it never contacts a camera or invokes an
updater.

Requires Python 3.10+. `patch` also needs a C compiler and uses the
public-domain LZMA SDK subset in tools/hi3861_lzma/ (see its README and
PROVENANCE.json).

Examples (after `dav.py unpack-app` extracts the camera app filesystem):
    python tools/hi3861_ota.py info app-root/Hi3861L_ezviz_sdk_ota.bin
    python tools/hi3861_ota.py unpack app-root/Hi3861L_ezviz_sdk_ota.bin \\
        --out hi3861-unpacked
    python tools/hi3861_ota.py patch app-root/Hi3861L_ezviz_sdk_ota.bin \\
        --out hi3861-patch
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
LZMA_HELPERS = HERE / "hi3861_lzma"

MAGIC = bytes.fromhex("1e96783c")
COMMON_HEADER_END = 0x180
COMMON_SIGNATURE = 0x180
SECTION_HEADER = 0x280
SECTION_SIGNATURE = 0x2c0
LZMA_OFFSET = 0x3c0
SIGNATURE_SLOT = 0x100
SHA256_LEN = 32
MAX_EXPANDED = 32 * 1024 * 1024

# The documented CP4 Hi3861L application image, version 2.139.
SOURCE_SHA256 = "e01c4867892f611326c4935abdcfb1d9a6c19629f2953b0cd441fb36578596c2"
SOURCE_EXPANDED_SHA256 = "ca5e1a57ff4b4f056f1a279fc0e902605126fd9651444ea9f84096d74bf6d788"
EXPECTED_VERSION = (2, 139)

PATCH_PLAN = HERE / "hi3861_wake" / "patch.json"
PATCHED_OTA_SHA256 = "951c3e569cd794821637263e805105c65b2e576cdd006f9028c4e3aea49679fb"
PATCHED_EXPANDED_SHA256 = "8d97d46ac40ed36fbf2dbfce593ab12998664a4e80e4756cb6fae960d764accd"

LZMA_LC, LZMA_LP, LZMA_PB = 2, 2, 2
LZMA_DICT_SIZE = 65536
LZMA_FAST_BYTES = 128
DECODER_CHUNKS = (1, 13, 64, 4096)


class Hi3861Error(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def _u64(data: bytes, offset: int) -> int:
    return struct.unpack_from("<Q", data, offset)[0]


def _check_signature_slot(data: bytes, label: str, content: bytes,
                          offset: int) -> None:
    slot = data[offset:offset + SIGNATURE_SLOT]
    if len(slot) != SIGNATURE_SLOT:
        raise Hi3861Error(f"{label} signature slot is truncated")
    if hashlib.sha256(content).digest() != slot[:SHA256_LEN]:
        raise Hi3861Error(f"{label} SHA-256 slot mismatch")
    if any(slot[SHA256_LEN:]):
        raise Hi3861Error(f"{label} signature slot has nonzero padding")


def validate_wrapper(data: bytes) -> None:
    """Validate one HiSilicon SHA-256 wrapper (outer OTA or expanded image)."""
    if len(data) < LZMA_OFFSET:
        raise Hi3861Error(f"image is only {len(data)} bytes")
    if data[:4] != MAGIC or data[SECTION_HEADER:SECTION_HEADER + 4] != MAGIC:
        raise Hi3861Error("not a Hi3861 update wrapper (bad magic)")
    section_offset = _u32(data, 0x08)
    section_len = _u32(data, 0x0C)
    if section_offset != SECTION_HEADER:
        raise Hi3861Error(
            f"unexpected section offset 0x{section_offset:x}, expected 0x280")
    if section_offset + section_len != len(data):
        raise Hi3861Error(
            f"section length 0x{section_len:x} does not match file size "
            f"{len(data)}")
    _check_signature_slot(data, "common header", data[:COMMON_HEADER_END],
                          COMMON_SIGNATURE)
    _check_signature_slot(
        data, "section",
        data[SECTION_HEADER:SECTION_SIGNATURE] + data[LZMA_OFFSET:],
        SECTION_SIGNATURE)


def rehash_wrapper(data: bytearray) -> None:
    """Recompute both SHA-256 slots of a wrapper in place."""
    data[COMMON_SIGNATURE:COMMON_SIGNATURE + SIGNATURE_SLOT] = (
        hashlib.sha256(data[:COMMON_HEADER_END]).digest()
        + bytes(SIGNATURE_SLOT - SHA256_LEN))
    data[SECTION_SIGNATURE:SECTION_SIGNATURE + SIGNATURE_SLOT] = (
        hashlib.sha256(
            data[SECTION_HEADER:SECTION_SIGNATURE] + data[LZMA_OFFSET:]).digest()
        + bytes(SIGNATURE_SLOT - SHA256_LEN))
    validate_wrapper(data)


def _parse_common_header(data: bytes) -> dict:
    param = _u32(data, 0x3C)
    return {
        "image_id": _u32(data, 0x00),
        "struct_version": _u32(data, 0x04),
        "section_offset": _u32(data, 0x08),
        "section_len": _u32(data, 0x0C),
        # The vendor version sits in the user_info area. The update policy
        # compares these two little-endian u16 values.
        "version_major": _u16(data, 0x18),
        "version_minor": _u16(data, 0x1A),
        "file_type": data[0x30],
        "file_version": data[0x31],
        "encrypt_flag": data[0x32],
        "file_attr": data[0x33],
        "file_len": _u32(data, 0x34),
        "key_len": _u32(data, 0x38),
        "hash_alg": param & 0xFFFF,
        "sign_alg": (param >> 16) & 0x3F,
        "sign_param": param >> 22,
    }


def _parse_section_header(data: bytes) -> dict:
    return {
        "image_id": _u32(data, 0x280),
        "struct_version": _u32(data, 0x284),
        "section_count": data[0x28C],
        "section0_compress": data[0x2A8],
        "section0_offset": _u32(data, 0x2AC),
        "section0_len": _u32(data, 0x2B0),
    }


def _lzma_properties(prop: int) -> dict:
    return {
        "properties_byte": f"0x{prop:02x}",
        "lc": prop % 9,
        "lp": (prop // 9) % 5,
        "pb": prop // 45,
    }


@dataclass
class Ota:
    raw: bytes
    expanded: bytes
    info: dict
    name: str = "<memory>"

    @classmethod
    def parse(cls, data: bytes, name: str = "<memory>") -> "Ota":
        info, expanded = _parse_image(data)
        return cls(data, expanded, info, name)

    @classmethod
    def read(cls, path: str | Path) -> "Ota":
        path = Path(path)
        return cls.parse(path.read_bytes(), str(path))


def _parse_image(data: bytes) -> tuple[dict, bytes]:
    validate_wrapper(data)
    common = _parse_common_header(data)
    section = _parse_section_header(data)

    offset = section["section0_offset"]
    size = section["section0_len"]
    if offset != LZMA_OFFSET or offset + size > len(data):
        raise Hi3861Error(
            f"invalid compressed section bounds offset=0x{offset:x} "
            f"size=0x{size:x}")
    stream = data[offset:offset + size]
    if len(stream) < 13:
        raise Hi3861Error("compressed section is shorter than an LZMA header")

    properties = _lzma_properties(stream[0])
    dict_size = _u32(stream, 1)
    declared_size = _u64(stream, 5)
    if declared_size == 0 or declared_size > MAX_EXPANDED:
        raise Hi3861Error(
            f"implausible expanded size {declared_size} in the LZMA header")

    try:
        decoder = lzma.LZMADecompressor(format=lzma.FORMAT_ALONE)
        expanded = decoder.decompress(stream, max_length=declared_size + 1)
    except lzma.LZMAError as error:
        raise Hi3861Error(f"LZMA decompression failed: {error}") from error
    if not decoder.eof or decoder.unused_data:
        raise Hi3861Error("LZMA stream did not end at the declared size")
    if len(expanded) != declared_size:
        raise Hi3861Error(
            f"expanded size {len(expanded)} does not match the LZMA header "
            f"({declared_size})")
    validate_wrapper(expanded)

    expanded_digest = sha256(expanded)
    if expanded_digest == SOURCE_EXPANDED_SHA256:
        known = "source-2.139"
    elif expanded_digest == PATCHED_EXPANDED_SHA256:
        known = "patched-2.139"
    else:
        known = None

    info = {
        "size": len(data),
        "sha256": sha256(data),
        "common": common,
        "section": section,
        "lzma": {
            **properties,
            "dict_size": dict_size,
            "declared_size": declared_size,
            "stream_offset": offset,
            "stream_size": size,
            "stream_sha256": sha256(stream),
            "trailing_padding": len(data) - offset - size,
        },
        "expanded": {
            "size": len(expanded),
            "sha256": expanded_digest,
            "known_image": known,
        },
        "patch": {"protocol": "cp4", "state": "installed-design" if known == "patched-2.139" else "source" if known else "unknown"},
        "wrapper": {
            "common_header_sha256_ok": True,
            "section_sha256_ok": True,
        },
    }
    return info, expanded


def _type_text(value: int) -> str:
    names = {0xF0: "kernel", 0xE1: "boot"}
    return f"0x{value:02x} ({names.get(value, 'unknown')})"


def _sign_text(value: int) -> str:
    names = {0x00: "RSA-PKCS", 0x01: "RSA-PSS", 0x10: "ECDSA", 0x3F: "SHA-256"}
    return f"0x{value:02x} ({names.get(value, 'unknown')})"


def print_info(ota: Ota) -> None:
    info = ota.info
    common = info["common"]
    section = info["section"]
    lzma_info = info["lzma"]
    expanded = info["expanded"]
    patch = info["patch"]

    print(f"file        {ota.name}")
    print(f"size        {info['size']} bytes")
    print(f"sha256      {info['sha256']}")
    print(f"image_id    0x{common['image_id']:08x}")
    print(f"version     {common['version_major']}.{common['version_minor']}")
    print(f"file_type   {_type_text(common['file_type'])}")
    print(f"file_len    {common['file_len']}")
    print(f"encrypt     0x{common['encrypt_flag']:02x}"
          + (" (plaintext)" if common["encrypt_flag"] == 0x42 else ""))
    print(f"sign_alg    {_sign_text(common['sign_alg'])}")
    print(f"section     count={section['section_count']} "
          f"compress={section['section0_compress']} "
          f"offset=0x{section['section0_offset']:x} "
          f"len={section['section0_len']}")
    print(f"lzma        lc={lzma_info['lc']} lp={lzma_info['lp']} "
          f"pb={lzma_info['pb']} dict={lzma_info['dict_size']} "
          f"expanded={lzma_info['declared_size']} "
          f"padding={lzma_info['trailing_padding']}")
    print(f"expanded    {expanded['size']} bytes, sha256 {expanded['sha256']}"
          + (f" ({expanded['known_image']})" if expanded["known_image"] else ""))
    print("wrapper     common=OK section=OK")
    print(f"patch       {patch['state']} (independent UDP 50162 + OEM prewake)")


def _write_unpacked(ota: Ota, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    kernel = out / "kernel.bin"
    strings = out / "strings.txt"
    manifest = out / "metadata.json"

    kernel.write_bytes(ota.expanded)
    lines = [
        f"0x{match.start():08x} {match.group().decode('ascii')}"
        for match in re.finditer(rb"[ -~]{5,}", ota.expanded)
    ]
    strings.write_text("\n".join(lines) + "\n")
    metadata = dict(ota.info)
    metadata.update({
        "source": ota.name,
        "kernel": str(kernel),
        "strings": str(strings),
    })
    manifest.write_text(json.dumps(metadata, indent=2) + "\n")
    return {
        "kernel": str(kernel),
        "strings": str(strings),
        "manifest": str(manifest),
        "strings_count": len(lines),
    }


def _check_source(ota: Ota) -> None:
    if ota.info["sha256"] != SOURCE_SHA256:
        raise Hi3861Error(
            "source OTA SHA-256 does not match the documented CP4 2.139 image\n"
            f"  expected {SOURCE_SHA256}\n"
            f"  got      {ota.info['sha256']}\n"
            "The patch address and packaging rules are specific to this image.")
    if ota.info["expanded"]["sha256"] != SOURCE_EXPANDED_SHA256:
        raise Hi3861Error("expanded image does not match the documented 2.139 kernel")
    version = (ota.info["common"]["version_major"],
               ota.info["common"]["version_minor"])
    if version != EXPECTED_VERSION:
        raise Hi3861Error(
            f"MCU version {version[0]}.{version[1]} is not {EXPECTED_VERSION[0]}."
            f"{EXPECTED_VERSION[1]}")


def _apply_patch(expanded: bytes) -> bytes:
    plan = json.loads(PATCH_PLAN.read_text())
    patched = bytearray(expanded)
    allowed = set(range(SECTION_SIGNATURE, SECTION_SIGNATURE + SHA256_LEN))
    for region in plan["regions"]:
        offset = region["offset"]
        data = bytes.fromhex(region["bytes"])
        if len(data) != region["capacity"]:
            raise Hi3861Error("invalid receiver region length")
        patched[offset:offset + len(data)] = data
        allowed.update(range(offset, offset + len(data)))
    for hook in plan["hooks"]:
        offset = int(hook["at"], 16) - 0x40d000
        before, after = bytes.fromhex(hook["before"]), bytes.fromhex(hook["after"])
        if patched[offset:offset+len(before)] != before or len(before) != len(after):
            raise Hi3861Error("hook instruction precondition failed")
        patched[offset:offset+len(after)] = after
        allowed.update(range(offset, offset+len(after)))
    rehash_wrapper(patched)
    if len(patched) != len(expanded) or any(i not in allowed for i,(a,b) in enumerate(zip(expanded,patched)) if a != b):
        raise Hi3861Error("unexpected expanded-image difference")
    if sha256(patched) != PATCHED_EXPANDED_SHA256:
        raise Hi3861Error("receiver code does not match pinned expanded image")
    return bytes(patched)


def _compile_helper(output: Path, source: Path, include: Path,
                    extra_sources: list[Path],
                    defines: tuple[str, ...] = ()) -> None:
    compiler = shutil.which(os.environ.get("CC", "cc"))
    if compiler is None:
        raise Hi3861Error(
            "no C compiler found; install clang or gcc, or set CC")
    command = [
        compiler, "-O2", *defines, f"-I{include}", str(source),
        *(str(item) for item in extra_sources), "-o", str(output),
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise Hi3861Error(
            "failed to compile the LZMA helper:\n"
            + (result.stderr or result.stdout).strip())


def _build_helpers(tmp: Path) -> tuple[Path, Path]:
    sdk19 = LZMA_HELPERS / "sdk19"
    sdk920 = LZMA_HELPERS / "sdk920"
    for directory in (sdk19, sdk920):
        if not directory.is_dir():
            raise Hi3861Error(f"missing LZMA SDK sources: {directory}")
    encoder = tmp / "encode"
    decoder = tmp / "decode"
    _compile_helper(encoder, LZMA_HELPERS / "encode.c", sdk19,
                    [sdk19 / "LzmaEnc.c", sdk19 / "LzFind.c",
                     sdk19 / "CpuArch.c"],
                    defines=("-D_7ZIP_ST",))
    _compile_helper(decoder, LZMA_HELPERS / "decode.c", sdk920,
                    [sdk920 / "LzmaDec.c"])
    return encoder, decoder


def _run_encoder(encoder: Path, kernel: bytes, tmp: Path) -> bytes:
    source = tmp / "input.bin"
    output = tmp / "output.lzma"
    source.write_bytes(kernel)
    result = subprocess.run(
        [str(encoder), str(source), str(output)],
        capture_output=True, text=True)
    if result.returncode:
        raise Hi3861Error(
            "the LZMA encoder failed:\n"
            + (result.stderr or result.stdout).strip())
    return output.read_bytes()


def _independent_decode(decoder: Path, stream: bytes, expected: bytes,
                        tmp: Path) -> list[dict]:
    stream_path = tmp / "stream.lzma"
    output_path = tmp / "decoded.bin"
    stream_path.write_bytes(stream)
    results = []
    for chunk in DECODER_CHUNKS:
        result = subprocess.run(
            [str(decoder), str(stream_path), str(output_path), str(chunk)],
            capture_output=True, text=True)
        if result.returncode:
            raise Hi3861Error(
                f"independent decoder failed with chunk {chunk}:\n"
                + (result.stderr or result.stdout).strip())
        if output_path.read_bytes() != expected:
            raise Hi3861Error(
                f"independent decoder output differs with chunk {chunk}")
        results.append(json.loads(result.stdout))
    return results


def _assemble(source: bytes, stream: bytes) -> bytes:
    outer = bytearray(source[:LZMA_OFFSET]) + stream
    outer += bytes((-len(outer)) % 16)
    struct.pack_into("<I", outer, 0x0C, len(outer) - SECTION_HEADER)
    struct.pack_into("<I", outer, 0x34, len(outer))
    struct.pack_into("<I", outer, 0x2B0, len(stream))
    rehash_wrapper(outer)
    return bytes(outer)


def cmd_info(args: argparse.Namespace) -> int:
    ota = Ota.read(args.image)
    if args.json:
        info = dict(ota.info)
        info["source"] = ota.name
        print(json.dumps(info, indent=2))
    else:
        print_info(ota)
    return 0


def cmd_unpack(args: argparse.Namespace) -> int:
    ota = Ota.read(args.image)
    out = Path(args.out)
    result = _write_unpacked(ota, out)
    print(f"unpacked    {ota.name}")
    print(f"kernel      {result['kernel']} ({len(ota.expanded)} bytes)")
    print(f"strings     {result['strings']} ({result['strings_count']} strings)")
    print(f"manifest    {result['manifest']}")
    return 0


def cmd_patch(args: argparse.Namespace) -> int:
    source = Ota.read(args.image)
    _check_source(source)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    ota_path = out / "Hi3861L_CP4_independent_wake_ota.bin"
    kernel_path = out / "kernel.bin"
    manifest_path = out / "manifest.json"

    print("source checks: OK", file=sys.stderr)
    with tempfile.TemporaryDirectory(prefix="hi3861-lzma-") as tmp_name:
        tmp = Path(tmp_name)
        encoder, decoder = _build_helpers(tmp)
        print("native LZMA helpers: built", file=sys.stderr)

        section = source.info["section"]
        oem_stream = source.raw[
            section["section0_offset"]:
            section["section0_offset"] + section["section0_len"]]
        control = _run_encoder(encoder, source.expanded, tmp)
        if control != oem_stream:
            raise Hi3861Error(
                "the encoder did not reproduce the OEM compressed stream; "
                "refusing to build a differently packaged image")
        print("OEM stream control: byte-identical", file=sys.stderr)
        control_tests = _independent_decode(
            decoder, control, source.expanded, tmp)

        patched_kernel = _apply_patch(source.expanded)
        patched_stream = _run_encoder(encoder, patched_kernel, tmp)
        if patched_stream[:13] != oem_stream[:13]:
            raise Hi3861Error(
                "the patched stream's LZMA header differs from the OEM header")
        patched_tests = _independent_decode(
            decoder, patched_stream, patched_kernel, tmp)
        print("independent SDK 9.20 decoding: OK", file=sys.stderr)

        outer = _assemble(source.raw, patched_stream)
        if len(outer) != 552608:
            raise Hi3861Error("unexpected receiver OTA size")
        if sha256(outer) != PATCHED_OTA_SHA256:
            raise Hi3861Error(
                "the rebuilt OTA does not match the documented patched hash\n"
                f"  expected {PATCHED_OTA_SHA256}\n"
                f"  got      {sha256(outer)}")
        if sha256(patched_kernel) != PATCHED_EXPANDED_SHA256:
            raise Hi3861Error(
                "the patched kernel does not match the documented hash")
        print("documented patched SHA-256: match", file=sys.stderr)

        rebuilt = Ota.parse(outer, str(ota_path))
        if rebuilt.expanded != patched_kernel:
            raise Hi3861Error("rebuild/decompress round trip differs")
        if rebuilt.info["lzma"]["trailing_padding"] != 12:
            raise Hi3861Error("unexpected payload padding after the patched stream")

        # A one-byte change to the compressed payload must be rejected by the
        # outer section hash.
        bad = bytearray(outer)
        bad[LZMA_OFFSET + 8] ^= 1
        try:
            validate_wrapper(bad)
        except Hi3861Error:
            pass
        else:
            raise Hi3861Error("a corrupted payload was accepted")

    ota_path.write_bytes(outer)
    kernel_path.write_bytes(patched_kernel)

    changes = [
        index for index, (old, new) in enumerate(zip(source.expanded,
                                                     patched_kernel))
        if old != new
    ]
    manifest = {
        "status": "OFFLINE ONLY; not installed, flashed, or boot-verified",
        "warning": (
            "This is an experimental Hi3861L application-kernel update, not a "
            "camera .dav package. Installing it can leave the Wi-Fi MCU "
            "unbootable, and no validated UART/JTAG recovery exists for the "
            "tested unit. Read docs/HI3861-LAN-PATCH.md first."),
        "source": {
            "file": source.name,
            "size": source.info["size"],
            "sha256": source.info["sha256"],
            "expanded_sha256": source.info["expanded"]["sha256"],
            "version": (
                f"{source.info['common']['version_major']}."
                f"{source.info['common']['version_minor']}"),
        },
        "patch": {"protocol": "cp4", "udp_port": 50162,
                  "payload": "43503457414b45015003bd2f", "prewake_seconds": 20,
                  "plan": json.loads(PATCH_PLAN.read_text()),
                  "changed_offsets": [f"0x{i:x}" for i in changes]},
        "result": {
            "file": str(ota_path),
            "size": len(outer),
            "sha256": sha256(outer),
            "expanded_sha256": sha256(patched_kernel),
            "kernel_file": str(kernel_path),
            "trailing_padding": rebuilt.info["lzma"]["trailing_padding"],
        },
        "lzma": {
            "encoder": "LZMA SDK 19.00",
            "lc": LZMA_LC,
            "lp": LZMA_LP,
            "pb": LZMA_PB,
            "dict_size": LZMA_DICT_SIZE,
            "fast_bytes": LZMA_FAST_BYTES,
            "end_marker": False,
            "control_stream_sha256": sha256(control),
            "patched_stream_size": len(patched_stream),
            "oem_stream_size": len(oem_stream),
            "control_decoder_tests": control_tests,
            "patched_decoder_tests": patched_tests,
        },
        "checks": [
            "source identity and version",
            "original instruction bytes",
            "payload diff allowlist",
            "inner and outer SHA-256",
            "OEM compressed stream reproduced byte-for-byte",
            "LZMA header preserved",
            "pinned OTA size and regenerated wrapper lengths/hashes",
            "rebuild/decompress round trip",
            "corrupted payload rejected",
            "independent SDK 9.20 decoding at 1/13/64/4096-byte input chunks",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print(json.dumps(manifest, indent=2))
    print(f"wrote {ota_path}", file=sys.stderr)
    print("OFFLINE BUILD ONLY; no device was contacted and nothing was installed.",
          file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="validate and describe an OTA image")
    p.add_argument("image", help="Hi3861L_ezviz_sdk_ota.bin")
    p.add_argument("--json", action="store_true", help="print JSON")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("unpack", help="expand the kernel and write metadata")
    p.add_argument("image", help="Hi3861L_ezviz_sdk_ota.bin")
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser(
        "patch", help="build the independent UDP receiver with OEM prewake")
    p.add_argument("image", help="the exact CP4 2.139 Hi3861L OTA")
    p.add_argument("--out", required=True, help="output directory")
    p.set_defaults(func=cmd_patch)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (Hi3861Error, OSError, ValueError, lzma.LZMAError,
            struct.error, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

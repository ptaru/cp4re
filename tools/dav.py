#!/usr/bin/env python3
"""Inspect, verify, unpack, and rebuild EZVIZ SWKH .dav firmware containers.

This is an offline tool. It never contacts a camera or the EZVIZ cloud.

The container layout was recovered from CP4 firmware V5.3.3 build 251118:

    0x00  460-byte header, XOR-obfuscated with a fixed 16-byte key
          <4I  magic "SWKH", additive checksum, header length, entry count
    0x40  entry table (44 bytes per entry)
          <32s name, <I offset, <I size, <I additive byte sum
    ...   plaintext component payloads, packed back to back

Checksums are 32-bit additive byte sums. They detect corruption and are not a
vendor signature. The header checksum covers decoded bytes [0xc:header_length].

The app component (app.img) is itself a SquashFS image padded to 4096 bytes
followed by a 128-byte SWKH "extra info" trailer. See append_trailer().

Run "dav.py --help" or "dav.py <command> --help".
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import struct
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

XOR_KEY = bytes.fromhex("BACDBCFED6CADDD3BAB9A3ABBFCBB5BE")
HEADER_PREFIX = 64
MIN_ENTRY = 44
TRAILER_SIZE = 0x80
TRAILER_ALIGN = 0x1000
DEFAULT_APP_PARTITION = 0x600000


class DavError(RuntimeError):
    pass


def xor_codec(data: bytes) -> bytes:
    """XOR-obfuscate or de-obfuscate a header region. The operation is symmetric."""
    return bytes(v ^ XOR_KEY[(i + (i >> 4)) & 15] for i, v in enumerate(data))


def additive_sum(data: bytes) -> int:
    return sum(data) & 0xFFFFFFFF


def _safe_component_name(name: str) -> str:
    if not name or name in (".", "..") or "\0" in name:
        raise DavError(f"unsafe component name {name!r}")
    if "/" in name or "\\" in name:
        raise DavError(f"component name contains a path separator: {name!r}")
    return name


@dataclass
class Component:
    name: str
    offset: int
    size: int
    checksum: int
    data: bytes
    offset_in_file: int = 0

    @property
    def checksum_ok(self) -> bool:
        return additive_sum(self.data) == self.checksum

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "offset": self.offset,
            "size": self.size,
            "checksum": f"0x{self.checksum:08x}",
            "checksum_ok": self.checksum_ok,
            "sha256": hashlib.sha256(self.data).hexdigest(),
        }


@dataclass
class Dav:
    raw: bytes
    header: bytes
    header_length: int
    entry_width: int
    secure_flag: int
    firm_sec: int
    platform: str
    components: list[Component] = field(default_factory=list)
    trailing: int = 0

    @classmethod
    def parse(cls, data: bytes) -> "Dav":
        if len(data) < HEADER_PREFIX:
            raise DavError("file is shorter than the 64-byte header")
        head = xor_codec(data[:HEADER_PREFIX])
        if head[:4] != b"SWKH":
            raise DavError("not an SWKH container (bad magic)")
        _, checksum, length, count = struct.unpack_from("<4I", head)
        if not (HEADER_PREFIX <= length <= min(1 << 20, len(data))):
            raise DavError(f"implausible header length {length}")
        if not (0 < count <= 1024):
            raise DavError(f"implausible entry count {count}")
        if (length - HEADER_PREFIX) % count:
            raise DavError("header length is not a whole number of entries")
        width = (length - HEADER_PREFIX) // count
        if width < MIN_ENTRY:
            raise DavError(f"implausible entry width {width}")
        header = xor_codec(data[:length])
        calculated = additive_sum(header[12:length])
        if calculated != checksum:
            raise DavError(
                f"header checksum mismatch (stored 0x{checksum:08x}, "
                f"calculated 0x{calculated:08x})"
            )
        components = []
        end = 0
        seen = set()
        for index in range(count):
            item = header[HEADER_PREFIX + index * width:HEADER_PREFIX + (index + 1) * width]
            name = item[:32].split(b"\0")[0].decode("ascii", "replace")
            _safe_component_name(name)
            if name in seen:
                raise DavError(f"duplicate component {name!r}")
            seen.add(name)
            offset, size, stored = struct.unpack_from("<III", item, 32)
            if offset + size > len(data):
                raise DavError(f"component {name!r} extends past end of file")
            end = max(end, offset + size)
            components.append(
                Component(name, offset, size, stored, data[offset:offset + size])
            )
        return cls(
            raw=data,
            header=header,
            header_length=length,
            entry_width=width,
            secure_flag=header[0x24],
            firm_sec=header[0x25] & 3,
            platform=header[0x2C:0x40].split(b"\0")[0].decode("ascii", "replace"),
            components=components,
            trailing=len(data) - end,
        )

    @classmethod
    def read(cls, path: str | Path) -> "Dav":
        return cls.parse(Path(path).read_bytes())

    def component(self, name: str) -> Component:
        for item in self.components:
            if item.name == name:
                return item
        raise DavError(f"component {name!r} not found")

    def problems(self, roundtrip: bool = False) -> list[str]:
        found = []
        for item in self.components:
            if not item.checksum_ok:
                found.append(f"component {item.name!r} checksum mismatch")
        if self.trailing:
            found.append(f"{self.trailing} trailing byte(s) after the last component")
        if roundtrip:
            encoded = xor_codec(self.header)
            if encoded != self.raw[:self.header_length]:
                found.append("header does not round-trip byte-for-byte")
        return found

    def rebuild(self, replacements: dict[str, bytes]) -> bytes:
        """Return a new container with the named components replaced.

        The original 64-byte header prefix is preserved, including the secure
        and encryption flag bytes. Entry offsets, entry checksums, and the
        header checksum are recomputed. Only whole-component replacement is
        supported; the component list and entry width do not change.
        """
        for name in replacements:
            self.component(name)
        prefix = bytearray(self.header[:HEADER_PREFIX])
        table = bytearray()
        payload = bytearray()
        position = self.header_length
        for item in self.components:
            data = replacements.get(item.name, item.data)
            table += item.name.encode("ascii")[:31].ljust(32, b"\0")
            table += struct.pack("<III", position, len(data), additive_sum(data))
            payload += data
            position += len(data)
        new_header = prefix + table
        if len(new_header) != self.header_length:
            raise DavError("internal error: rebuilt header changed size")
        struct.pack_into("<I", new_header, 4, 0)
        struct.pack_into("<I", new_header, 4, additive_sum(new_header[12:]))
        return xor_codec(bytes(new_header)) + bytes(payload)

    def as_dict(self) -> dict:
        return {
            "size": len(self.raw),
            "header_length": self.header_length,
            "entry_width": self.entry_width,
            "secure_flag": f"0x{self.secure_flag:02x}",
            "firm_sec": self.firm_sec,
            "platform": self.platform,
            "components": [item.as_dict() for item in self.components],
            "trailing": self.trailing,
        }


# ---------------------------------------------------------------------------
# app.img trailing block
# ---------------------------------------------------------------------------


def read_trailer(app_image: bytes) -> dict | None:
    """Return the parsed 128-byte SWKH trailer at the end of app.img, if any."""
    if len(app_image) < TRAILER_SIZE or app_image[-TRAILER_SIZE:-TRAILER_SIZE + 4] != b"SWKH":
        return None
    trailer = app_image[-TRAILER_SIZE:]
    stored = struct.unpack_from("<I", trailer, 4)[0]
    calculated = additive_sum(app_image[:-TRAILER_SIZE])
    return {
        "stored": stored,
        "calculated": calculated,
        "ok": stored == calculated,
        "tag": trailer[8:0x18].split(b"\0")[0].decode("ascii", "replace"),
        "version": trailer[0x18:0x20].split(b"\0")[0].decode("ascii", "replace"),
        "raw": trailer,
    }


def build_trailer(template_trailer: bytes, image: bytes, version: str | None) -> bytes:
    """Build a trailer for image, reusing the tag/padding from a known-good one."""
    if len(template_trailer) != TRAILER_SIZE or template_trailer[:4] != b"SWKH":
        raise DavError("template does not start with a 128-byte SWKH trailer")
    trailer = bytearray(template_trailer)
    struct.pack_into("<I", trailer, 4, additive_sum(image))
    if version:
        raw = version.encode("ascii")
        if len(raw) > 8:
            raise DavError("version string must fit in 8 bytes")
        trailer[0x18:0x20] = raw.ljust(8, b"\0")
    return bytes(trailer)


def append_trailer(
    original_app: str | Path,
    squashfs_image: str | Path,
    output: str | Path,
    version: str | None = None,
    partition_size: int = DEFAULT_APP_PARTITION,
) -> dict:
    """Pad a SquashFS image to 4096 bytes and append a refreshed SWKH trailer.

    original_app supplies the 128-byte trailer template (tag and padding). The
    image sum at trailer+4 is recomputed. The result must fit the app
    partition, including the trailer.
    """
    original = Path(original_app).read_bytes()
    parsed = read_trailer(original)
    if not parsed:
        raise DavError("original app image has no trailing SWKH block")
    image = Path(squashfs_image).read_bytes()
    image += b"\0" * ((-len(image)) % TRAILER_ALIGN)
    total = len(image) + TRAILER_SIZE
    if total > partition_size:
        raise DavError(
            f"app image {total} bytes exceeds the {partition_size}-byte partition"
        )
    trailer = build_trailer(parsed["raw"], image, version)
    Path(output).write_bytes(image + trailer)
    return {
        "output": str(output),
        "squashfs_size": len(image),
        "trailer_size": TRAILER_SIZE,
        "total": total,
        "free": partition_size - total,
        "version": trailer[0x18:0x20].split(b"\0")[0].decode("ascii", "replace"),
        "image_sum": f"0x{struct.unpack_from('<I', trailer, 4)[0]:08x}",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_info(dav: Dav) -> None:
    print(f"file        {len(dav.raw)} bytes (0x{len(dav.raw):x})")
    print(f"header      len={dav.header_length} entry_width={dav.entry_width} "
          f"entries={len(dav.components)}")
    print(f"flags       secure_flag=0x{dav.secure_flag:02x} "
          f"(security firmware expects 0x87) firm_sec={dav.firm_sec}")
    print(f"platform    {dav.platform!r}")
    print("components")
    for item in dav.components:
        state = "OK" if item.checksum_ok else "BAD"
        print(f"  {item.name:16s} off=0x{item.offset:08x} size={item.size:>9d} "
              f"sum=0x{item.checksum:08x} {state}")
        trailer = read_trailer(item.data)
        if trailer:
            print(f"      SWKH trailer: tag={trailer['tag']!r} "
                  f"version={trailer['version']!r} {state.lower()}")
    print(f"trailing    {dav.trailing} byte(s)")


def cmd_info(args: argparse.Namespace) -> int:
    dav = Dav.read(args.file)
    if args.json:
        print(json.dumps(dav.as_dict(), indent=2))
    else:
        _print_info(dav)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    dav = Dav.read(args.file)
    _print_info(dav)
    problems = dav.problems(roundtrip=args.roundtrip)
    if args.roundtrip:
        mismatch = "header does not round-trip byte-for-byte" in problems
        print(f"roundtrip   {'MISMATCH' if mismatch else 'byte-identical'}")
    print(f"result      {'all checks passed' if not problems else 'PROBLEMS'}")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


def cmd_unpack(args: argparse.Namespace) -> int:
    dav = Dav.read(args.file)
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = dav.as_dict()
    for item in dav.components:
        if not args.allow_bad and not item.checksum_ok:
            raise DavError(f"refusing to unpack {item.name!r}: checksum mismatch")
        (out / item.name).write_bytes(item.data)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"unpacked {len(dav.components)} component(s) to {out}")
    print(f"manifest {out / 'manifest.json'}")
    return 0


def cmd_repack(args: argparse.Namespace) -> int:
    dav = Dav.read(args.base)
    replacements = {}
    for spec in args.replace:
        if "=" not in spec:
            raise DavError(f"--replace expects NAME=FILE, got {spec!r}")
        name, path = spec.split("=", 1)
        replacements[name] = Path(path).read_bytes()
    data = dav.rebuild(replacements)
    Path(args.output).write_bytes(data)
    rebuilt = Dav.parse(data)
    problems = rebuilt.problems(roundtrip=True)
    print(f"wrote {args.output} ({len(data)} bytes)")
    print(f"replaced: {', '.join(replacements) or '(none)'}")
    print(f"verify  : {'OK' if not problems else 'PROBLEMS'}")
    for problem in problems:
        print(f"  - {problem}")
    return 1 if problems else 0


def cmd_diff(args: argparse.Namespace) -> int:
    left = Dav.read(args.left)
    right = Dav.read(args.right)
    left_names = {item.name for item in left.components}
    right_names = {item.name for item in right.components}
    problems = 0
    for name in sorted(left_names | right_names):
        if name not in left_names or name not in right_names:
            print(f"{name:16s} only in {'right' if name in right_names else 'left'}")
            problems += 1
            continue
        a, b = left.component(name), right.component(name)
        changed = a.checksum != b.checksum or a.size != b.size
        print(f"{name:16s} {'CHANGED' if changed else 'same':7s} "
              f"{a.size} -> {b.size}")
        problems += 1 if changed else 0
    header_changes = []
    for label, x, y in (
        ("secure_flag", left.secure_flag, right.secure_flag),
        ("firm_sec", left.firm_sec, right.firm_sec),
        ("platform", left.platform, right.platform),
    ):
        if x != y:
            header_changes.append(f"{label}: {x!r} -> {y!r}")
    if header_changes:
        print("header changes:")
        for line in header_changes:
            print(f"  {line}")
    print(f"result {'identical payloads' if not problems else f'{problems} component(s) differ'}")
    return 0


def cmd_trailer(args: argparse.Namespace) -> int:
    result = append_trailer(
        args.original_app,
        args.squashfs,
        args.output,
        version=args.version,
        partition_size=args.partition_size,
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_unpack_app(args: argparse.Namespace) -> int:
    data = Path(args.app_image).read_bytes()
    trailer = read_trailer(data)
    if not trailer:
        raise DavError("app image has no trailing SWKH block")
    if not trailer["ok"]:
        raise DavError("app image trailer checksum mismatch")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        squash = Path(tmp) / "app.squashfs"
        squash.write_bytes(data[:-TRAILER_SIZE])
        if args.keep_squashfs:
            shutil.copyfile(squash, args.keep_squashfs)
        tool = shutil.which(args.unsquashfs)
        if not tool:
            raise DavError(
                f"{args.unsquashfs!r} not found; install squashfs-tools or use "
                f"--keep-squashfs to save the image and extract it elsewhere"
            )
        subprocess.run([tool, "-d", str(outdir), str(squash)], check=True)
    print(f"unpacked {args.app_image} (trailer version {trailer['version']!r}) to {outdir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="print header, flags, components and trailers")
    p.add_argument("file")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("verify", help="validate all checksums")
    p.add_argument("file")
    p.add_argument("--roundtrip", action="store_true",
                   help="also require the header to re-encode byte-identically")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("unpack", help="extract components and a manifest")
    p.add_argument("file")
    p.add_argument("outdir")
    p.add_argument("--allow-bad", action="store_true",
                   help="extract even if a component checksum fails")
    p.set_defaults(func=cmd_unpack)

    p = sub.add_parser("repack", help="replace one or more components")
    p.add_argument("base")
    p.add_argument("output")
    p.add_argument("--replace", action="append", default=[], metavar="NAME=FILE",
                   help="replace component NAME with the contents of FILE")
    p.set_defaults(func=cmd_repack)

    p = sub.add_parser("diff", help="compare two containers by component checksum")
    p.add_argument("left")
    p.add_argument("right")
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("trailer", help="pad a SquashFS image and append the app trailer")
    p.add_argument("original_app", help="known-good app.img that supplies the trailer template")
    p.add_argument("squashfs", help="new SquashFS image (without trailer)")
    p.add_argument("output")
    p.add_argument("--version", help="trailer version string, at most 8 bytes")
    p.add_argument("--partition-size", type=lambda s: int(s, 0),
                   default=DEFAULT_APP_PARTITION)
    p.set_defaults(func=cmd_trailer)

    p = sub.add_parser("unpack-app", help="extract app.img with unsquashfs")
    p.add_argument("app_image")
    p.add_argument("outdir")
    p.add_argument("--unsquashfs", default="unsquashfs")
    p.add_argument("--keep-squashfs", help="also save the trailer-stripped SquashFS here")
    p.set_defaults(func=cmd_unpack_app)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except (DavError, OSError, struct.error, UnicodeError,
            subprocess.CalledProcessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

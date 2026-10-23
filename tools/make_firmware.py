#!/usr/bin/env python3
"""Build a modified EZVIZ SWKH firmware package from a base .dav.

Typical use: replace app.img with a SquashFS built from an extracted app
filesystem. The tool reproduces the CP4 build sequence:

    1. mksquashfs <app-root> <image> -comp lzo -b 131072 ...
    2. pad to 4096 bytes and append the 128-byte SWKH app trailer
    3. replace app.img in the base container and recompute both checksums
    4. verify the result

It also builds a recovery package: the base's original app.img with only the
trailer version changed.

This is an offline build tool. It does not install anything.

Example:
    python tools/make_firmware.py \\
        --base official.dav --app-root /tmp/app-root \\
        --version 5.3.16 --out custom.dav

    python tools/make_firmware.py \\
        --base official.dav --recovery --version 5.3.17 --out recovery.dav
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from dav import (
    DEFAULT_APP_PARTITION,
    TRAILER_SIZE,
    Dav,
    DavError,
    append_trailer,
    build_trailer,
    read_trailer,
)

DEFAULT_COMPONENT = "app.img"
DEFAULT_COMP = "lzo"
DEFAULT_BLOCK_SIZE = 131072


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def strip_trailer(image: bytes) -> bytes:
    trailer = read_trailer(image)
    if trailer:
        if not trailer["ok"]:
            raise DavError("existing app trailer checksum mismatch")
        return image[:-TRAILER_SIZE]
    return image


def make_squashfs(app_root: Path, output: Path, tool: str, comp: str,
                  block_size: int) -> None:
    command = [
        tool, str(app_root), str(output),
        "-comp", comp,
        "-b", str(block_size),
        "-noappend",
        "-all-root",
        "-no-xattrs",
        "-no-progress",
    ]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        raise DavError(
            "mksquashfs failed:\n" + (result.stderr or result.stdout).strip()
        )


def build_app_image(args: argparse.Namespace, original_app: bytes) -> bytes:
    """Return a complete app.img (SquashFS + refreshed 128-byte trailer)."""
    template = read_trailer(original_app)
    if not template:
        raise DavError("base app.img has no SWKH trailer to use as a template")

    if args.recovery:
        body = strip_trailer(original_app)
    elif args.app_image:
        body = strip_trailer(Path(args.app_image).read_bytes())
    else:
        if not args.app_root:
            raise DavError("supply --app-root, --app-image, or --recovery")
        app_root = Path(args.app_root)
        if not app_root.is_dir():
            raise DavError(f"app root is not a directory: {app_root}")
        tool = shutil.which(args.mksquashfs)
        if not tool:
            raise DavError(
                f"{args.mksquashfs!r} not found; install squashfs-tools"
            )
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "app.squashfs"
            make_squashfs(app_root, image, tool, args.comp, args.block_size)
            body = image.read_bytes()
        print(f"mksquashfs  {len(body)} bytes ({args.comp}, "
              f"block {args.block_size})")

    body += b"\0" * ((-len(body)) % 0x1000)
    total = len(body) + TRAILER_SIZE
    if total > args.partition_size:
        raise DavError(
            f"app image {total} bytes exceeds the "
            f"{args.partition_size}-byte partition by "
            f"{total - args.partition_size}"
        )
    version = None if args.keep_trailer_version else args.version
    trailer = build_trailer(template["raw"], body, version)
    image = body + trailer
    version_text = trailer[0x18:0x20].split(b"\0")[0].decode("ascii", "replace")
    print(f"app.img     {len(image)} bytes, trailer version {version_text!r}, "
          f"{args.partition_size - len(image)} bytes free")
    return image


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--base", required=True,
                        help="known-good .dav whose non-app components are kept")
    parser.add_argument("--out", required=True, help="output .dav")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--app-root", help="extracted app filesystem to mksquashfs")
    source.add_argument("--app-image", help="prebuilt app.img or SquashFS image")
    source.add_argument("--recovery", action="store_true",
                        help="keep the base's original app contents")
    parser.add_argument("--version", help="new app trailer version (max 8 bytes)")
    parser.add_argument("--keep-trailer-version", action="store_true",
                        help="leave the trailer version byte-for-byte unchanged")
    parser.add_argument("--component", default=DEFAULT_COMPONENT,
                        help=f"component to replace (default {DEFAULT_COMPONENT})")
    parser.add_argument("--partition-size", type=lambda s: int(s, 0),
                        default=DEFAULT_APP_PARTITION)
    parser.add_argument("--mksquashfs", default="mksquashfs")
    parser.add_argument("--comp", default=DEFAULT_COMP)
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE)
    args = parser.parse_args()

    if not args.recovery and not args.keep_trailer_version and not args.version:
        parser.error("--version is required unless --keep-trailer-version is used")

    try:
        if args.component != DEFAULT_COMPONENT:
            raise DavError(
                f"this tool only knows how to refresh the {DEFAULT_COMPONENT} "
                f"trailer; use dav.py repack for {args.component!r}"
            )
        base = Dav.read(args.base)
        original_app = base.component(args.component).data
        app_image = build_app_image(args, original_app)
        data = base.rebuild({args.component: app_image})
        out = Path(args.out)
        out.write_bytes(data)

        rebuilt = Dav.parse(data)
        problems = rebuilt.problems(roundtrip=True)
        app = rebuilt.component(DEFAULT_COMPONENT)
        trailer = read_trailer(app.data)
        report = {
            "output": str(out),
            "size": len(data),
            "sha256": sha256(out),
            "component": args.component,
            "component_size": len(app_image),
            "trailer_version": trailer["version"] if trailer else None,
            "trailer_ok": trailer["ok"] if trailer else None,
            "partition_free": args.partition_size - len(app_image),
            "problems": problems,
        }
        print(json.dumps(report, indent=2))
        if problems:
            print("BUILD VERIFICATION FAILED", file=sys.stderr)
            return 1
        print("build verification: OK")
        return 0
    except (DavError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Recover a complete local-file entry from a truncated ZIP archive.

Some download paths expose a ZIP progressively and omit the central directory
until the transfer finishes.  Python's ``zipfile`` cannot read such a file,
but the local headers still provide enough information to recover an entry.
The extractor streams deflate members, verifies the CRC and refuses a
truncated member.  It is intentionally limited to one explicitly named entry
so it cannot accidentally materialize an unbounded archive.
"""

from __future__ import annotations

import argparse
import binascii
from pathlib import Path
import struct
import zlib


LOCAL_HEADER = b"PK\x03\x04"
DATA_DESCRIPTOR = b"PK\x07\x08"


def _read_exact(handle, size: int) -> bytes:
    value = handle.read(size)
    if len(value) != size:
        raise ValueError("truncated ZIP local header")
    return value


def recover(archive: Path, member_name: str, destination: Path) -> int:
    with archive.open("rb") as handle:
        while True:
            offset = handle.tell()
            signature = handle.read(4)
            if not signature:
                break
            if signature != LOCAL_HEADER:
                raise ValueError(f"unexpected ZIP signature at offset {offset}")
            header = _read_exact(handle, 26)
            version, flags, method, _mtime, _mdate, expected_crc, compressed_size, uncompressed_size, name_size, extra_size = struct.unpack(
                "<HHHHHIIIHH", header
            )
            del version
            name = _read_exact(handle, name_size).decode("utf-8")
            handle.seek(extra_size, 1)
            if flags & 0x08:
                if method == 0:
                    raise ValueError(f"stored data descriptor is unsupported: {name}")
                decompressor = zlib.decompressobj(-15)
                output = bytearray()
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        raise ValueError(f"truncated deflate member: {name}")
                    output.extend(decompressor.decompress(chunk))
                    if decompressor.eof:
                        unused = decompressor.unused_data
                        handle.seek(-len(unused), 1)
                        break
                descriptor = _read_exact(handle, 16)
                if descriptor[:4] != DATA_DESCRIPTOR:
                    raise ValueError(f"missing data descriptor: {name}")
                expected_crc, compressed_size, uncompressed_size = struct.unpack(
                    "<III", descriptor[4:]
                )
            elif method == 0:
                output = bytearray(_read_exact(handle, compressed_size))
            elif method == 8:
                compressed = _read_exact(handle, compressed_size)
                output = bytearray(zlib.decompress(compressed, -15))
            else:
                raise ValueError(f"unsupported ZIP compression method {method}: {name}")

            if len(output) != uncompressed_size:
                raise ValueError(f"uncompressed size mismatch for {name}")
            actual_crc = binascii.crc32(output) & 0xFFFFFFFF
            if actual_crc != expected_crc:
                raise ValueError(f"CRC mismatch for {name}")
            if name == member_name:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(output)
                return len(output)
    raise FileNotFoundError(member_name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--member", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    size = recover(args.archive, args.member, args.output)
    print(f"recovered {args.member} ({size} bytes) -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

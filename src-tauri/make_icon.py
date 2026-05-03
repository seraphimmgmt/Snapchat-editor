"""Generate a 1024x1024 placeholder app icon using only stdlib."""
import struct, zlib, sys, pathlib

size = 1024
bg = (10, 10, 12)
fg = (255, 252, 0)
r2 = (size * 0.42) ** 2
cx = cy = size / 2

raw = bytearray()
for y in range(size):
    raw.append(0)
    row = bytearray()
    for x in range(size):
        d2 = (x - cx) ** 2 + (y - cy) ** 2
        if d2 < r2:
            row.extend(fg)
        else:
            row.extend(bg)
    raw.extend(row)

def chunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xffffffff))

png = b"\x89PNG\r\n\x1a\n"
png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
png += chunk(b"IEND", b"")

out = pathlib.Path(sys.argv[1])
out.write_bytes(png)
print(f"wrote {out} ({len(png)} bytes)")

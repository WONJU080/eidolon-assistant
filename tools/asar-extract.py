#!/usr/bin/env python3
"""Extract one file from an Electron app.asar archive (read-only)."""
import struct, json, sys, os

def header(path):
    with open(path, 'rb') as f:
        hdr = f.read(8)
        header_size = struct.unpack('<I', hdr[4:8])[0]
        buf = f.read(header_size)
    strlen = struct.unpack('<I', buf[4:8])[0]
    return json.loads(buf[8:8 + strlen].decode('utf8')), 8 + header_size

def find(root, target):
    node = root
    for part in target.split('/'):
        node = node['files'][part]
    return node

if __name__ == '__main__':
    asar, target, out = sys.argv[1], sys.argv[2], sys.argv[3]
    root, base = header(asar)
    n = find(root, target)
    if 'files' in n:
        sys.exit('is a directory')
    with open(asar, 'rb') as f:
        f.seek(base + int(n['offset']))
        data = f.read(int(n['size']))
    os.makedirs(os.path.dirname(out) or '.', exist_ok=True)
    with open(out, 'wb') as g:
        g.write(data)
    print(f'{target} -> {out} ({len(data)} bytes)')

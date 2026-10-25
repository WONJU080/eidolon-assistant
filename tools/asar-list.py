#!/usr/bin/env python3
"""List entries inside an Electron app.asar archive (no extraction)."""
import struct, json, sys

def load(path):
    with open(path, 'rb') as f:
        hdr = f.read(8)
        header_size = struct.unpack('<I', hdr[4:8])[0]
        buf = f.read(header_size)
    strlen = struct.unpack('<I', buf[4:8])[0]
    return json.loads(buf[8:8 + strlen].decode('utf8'))

def walk(node, prefix=''):
    for name, v in node.get('files', {}).items():
        p = f'{prefix}/{name}' if prefix else name
        if 'files' in v:
            yield from walk(v, p)
        else:
            yield p, v

if __name__ == '__main__':
    root = load(sys.argv[1])
    pat = sys.argv[2].lower() if len(sys.argv) > 2 else ''
    entries = list(walk(root))
    hits = [p for p, _ in entries if pat in p.lower()]
    print(f'total={len(entries)} matched={len(hits)}')
    for h in hits[:400]:
        print(h)

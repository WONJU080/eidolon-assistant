import fs from 'node:fs';
const file = process.argv[2];
const pat = (process.argv[3] || '').toLowerCase();
const fd = fs.openSync(file, 'r');
const sizeBuf = Buffer.alloc(8);
fs.readSync(fd, sizeBuf, 0, 8, 0);
// asar: 4-byte uint32 size of pickle, then uint32 header string length
const headerSize = sizeBuf.readUInt32LE(4);
const strLen = sizeBuf.readUInt32LE(4 + 4 + 4); // within pickle: size(4) + str length(4)? see below
const buf = Buffer.alloc(headerSize);
fs.readSync(fd, buf, 0, headerSize, 8);
// buf begins with uint32 payload size then uint32 string length
const len = buf.readUInt32LE(0);
const json = buf.subarray(4, 4 + len).toString('utf8');
const root = JSON.parse(json);
const out = [];
function walk(node, prefix) {
  for (const [name, v] of Object.entries(node.files || {})) {
    const p = prefix ? prefix + '/' + name : name;
    if (v.files) walk(v, p);
    else out.push(`${p}\t${v.size ?? ''}\t${v.unpacked ? 'unpacked' : ''}`);
  }
}
walk(root, '');
const filtered = pat ? out.filter(l => l.toLowerCase().includes(pat)) : out;
process.stdout.write(filtered.join('\n') + `\n--- total ${out.length}, matched ${filtered.length}\n`);

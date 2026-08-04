#!/usr/bin/env python3
"""Locate register-table name blocks in the decompressed .dt stream.

DBNames lists every table in the infobase. An earlier 120 MB window recovered
the Reference/Document/Enum names but no registers, so the block extends past
that window. This does a cheap byte-level sweep of the whole stream - no
tokenising - recording where each family of names appears so a later pass can
target the right region.

Streaming only: peak disk is the source file, peak RAM a single chunk.
"""
import re
import sys
import zlib
from collections import defaultdict
from pathlib import Path

S = Path('/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')
SRC = S / 'backup.dt'
CHUNK = 16 << 20
OVERLAP = 64          # names never exceed this; covers chunk-boundary splits

# 1C writes these as ASCII in the DBNames block.
FAMILIES = [b'AccRg', b'AccumRg', b'InfoRg', b'Reference', b'Document',
            b'Enum', b'Chrc', b'Const', b'Seq', b'Node', b'Task']
NAME_RE = re.compile(
    rb'(AccRgED|AccRgAT|AccRgCT|AccRg|AccumRgTn|AccumRgOpt|AccumRgT|AccumRg|'
    rb'InfoRgSL|InfoRg|Reference|DocumentJournal|Document|Enum|Chrc|Const|'
    rb'Seq|Node|Task)(\d+)')

first_at = {}
last_at = {}
counts = defaultdict(int)
objects = defaultdict(set)

fh = open(SRC, 'rb')
fh.seek(9)                                   # 8-byte magic + version byte
dobj = zlib.decompressobj(-15)
pos = 0
tail = b''
total_out = 0

while True:
    raw = fh.read(CHUNK)
    if not raw:
        break
    try:
        out = dobj.decompress(raw)
    except zlib.error as exc:
        sys.stderr.write(f'deflate error at {pos}: {exc}\n')
        break
    if not out:
        continue
    total_out += len(out)
    buf = tail + out

    for m in NAME_RE.finditer(buf):
        fam = m.group(1).decode()
        num = m.group(2).decode()
        off = pos - len(tail) + m.start()
        counts[fam] += 1
        objects[fam].add(num)
        if fam not in first_at:
            first_at[fam] = off
        last_at[fam] = off

    pos += len(buf) - OVERLAP if len(buf) > OVERLAP else 0
    tail = buf[-OVERLAP:] if len(buf) > OVERLAP else buf
    del buf, out

    if total_out % (2 << 30) < CHUNK:
        sys.stderr.write(f'  ...{total_out >> 30} GB decompressed, '
                         f'{sum(counts.values())} name hits\n')
        sys.stderr.flush()

fh.close()

report = {
    'decompressed_bytes': total_out,
    'decompressed_gb': round(total_out / (1 << 30), 2),
    'per_family': {
        fam: {
            'name_tokens': counts[fam],
            'distinct_objects': len(objects[fam]),
            'first_offset': first_at.get(fam),
            'last_offset': last_at.get(fam),
        }
        for fam in sorted(counts, key=lambda f: -counts[f])
    },
    'total_distinct_objects': sum(len(v) for v in objects.values()),
}

import json
(S / 'analysis').mkdir(exist_ok=True)
with open(S / 'analysis' / 'register_map.json', 'w') as out_fh:
    json.dump(report, out_fh, indent=1)

print(json.dumps(report, indent=1))

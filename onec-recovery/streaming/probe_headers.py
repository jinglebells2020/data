#!/usr/bin/env python3
"""Check whether table names appear inline as section headers in the data region.

If they do, a streaming pass can track the current table and attribute records
to it - which is what turns the 95% 'other' bucket into typed tables. If names
only cluster in the DBNames block, attribution needs the relational schema and
streaming cannot supply it.

Samples three windows well past the schema block and reports name density plus
surrounding bytes.
"""
import re
import zlib
from pathlib import Path

S = Path('/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')
SRC = S / 'backup.dt'
CHUNK = 16 << 20

NAME_RE = re.compile(
    rb'(AccRgED|AccRgAT|AccRgCT|AccRg|AccumRgTn|AccumRgOpt|AccumRgT|AccumRg|'
    rb'InfoRgSL|InfoRg|Reference|DocumentJournal|Document|Enum|Chrc|Const)(\d+)')

# (label, start, length) - past the 1.958-1.984 GB schema block
WINDOWS = [
    ('schema_block', 1_960_000_000, 24 << 20),
    ('data_3GB',     3_000_000_000, 24 << 20),
    ('data_6GB',     6_000_000_000, 24 << 20),
    ('data_9GB',     9_000_000_000, 24 << 20),
]

fh = open(SRC, 'rb')
fh.seek(9)
dobj = zlib.decompressobj(-15)
pos = 0
results = {}
pending = list(WINDOWS)
buf_for = {}

while pending:
    raw = fh.read(CHUNK)
    if not raw:
        break
    out = dobj.decompress(raw)
    if not out:
        continue
    start, end = pos, pos + len(out)

    for label, wstart, wlen in list(pending):
        wend = wstart + wlen
        if end <= wstart or start >= wend:
            continue
        lo = max(0, wstart - start)
        hi = min(len(out), wend - start)
        buf_for.setdefault(label, bytearray()).extend(out[lo:hi])
        if end >= wend:
            data = bytes(buf_for.pop(label))
            hits = list(NAME_RE.finditer(data))
            uniq = {m.group(0).decode() for m in hits}
            samples = []
            for m in hits[:6]:
                a = max(0, m.start() - 12)
                b = min(len(data), m.end() + 28)
                samples.append({
                    'name': m.group(0).decode(),
                    'context': data[a:b].hex(),
                })
            results[label] = {
                'window_bytes': len(data),
                'name_hits': len(hits),
                'distinct_names': len(uniq),
                'hits_per_mb': round(len(hits) / (len(data) / (1 << 20)), 1),
                'samples': samples,
            }
            pending.remove((label, wstart, wlen))
    pos = end
    del out

fh.close()

import json
print(json.dumps(results, indent=1)[:4000])

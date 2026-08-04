#!/usr/bin/env python3
"""Targeted capture of the DBNames schema block from the 1C dump.

The main extraction caps field counts, which truncates the DBNames record.
This pass tokenises a bounded window around the DBNames marker with no caps
and recovers the full internal table/field inventory.
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, '/workspace/altailabs-production/packages/onec-client')
sys.path.insert(0, '/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')

from dt_parser.v3_container import AsciiField, Utf16Field, _tokenize_mmap  # noqa: E402
from stream_extract import StreamView  # noqa: E402

S = Path('/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')

START = 1_930_000_000
WINDOW = 120 * 1024 * 1024

TBL_RE = re.compile(
    r'^(Document|Reference|Enum|InfoRg|AccumRg|AccumRgT|AccumRgOpt|AccRg|Acc|Chrc|'
    r'Const|Task|BPr|Node|Seq|DocumentJournal|ScheduleRg|CRg|VT)(\d+)(?:_(.+))?$')
FLD_RE = re.compile(r'^(Fld\d+|Date_Time|ID|Marked|Posted|Number|Code|Description|'
                    r'DateTime|Period|Recorder|LineNo|Splitter|Version|_.*)$')

view = StreamView(S / 'backup.dt', skip=START)
tables = Counter()
fields = Counter()
raw_names = []
end = START + WINDOW

for tok in _tokenize_mmap(view):
    if view.tell() > end:
        break
    if isinstance(tok, AsciiField):
        v = tok.value.strip()
        if not v:
            continue
        m = TBL_RE.match(v)
        if m:
            tables[m.group(1)] += 1
            raw_names.append(v)
        elif FLD_RE.match(v):
            fields[v.split('Fld')[0] if v.startswith('Fld') else v] += 1

view.close()

# distinct object ids per kind
per_kind = {}
for nm in raw_names:
    m = TBL_RE.match(nm)
    if m:
        per_kind.setdefault(m.group(1), set()).add(m.group(2))

out = {
    'window': [START, end],
    'table_name_tokens': dict(tables.most_common()),
    'distinct_objects_per_kind': {k: len(v) for k, v in sorted(per_kind.items())},
    'total_distinct_objects': sum(len(v) for v in per_kind.values()),
    'sample_names': raw_names[:60],
}
with open(S / 'analysis' / 'dbnames.json', 'w') as fh:
    json.dump(out, fh, ensure_ascii=False, indent=1)
print(json.dumps({k: out[k] for k in
                  ('distinct_objects_per_kind', 'total_distinct_objects')},
                 ensure_ascii=False, indent=1))
print('samples:', raw_names[:25])

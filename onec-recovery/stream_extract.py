#!/usr/bin/env python3
"""Stream-extract business records from a 1C v3 .dt dump.

The repo's tokenizer (dt_parser.v3_container) is written against an mmap of a
fully decompressed file. This dump decompresses to ~24 GB, which does not fit
in the container's disk allowance alongside the 6.6 GB source. This module
supplies a file-like adapter over a streaming zlib decompressor exposing the
mmap surface the tokenizer needs (read / tell / seek / size), so the same
tested tokenizer runs with no large intermediate on disk.

Output: per-table CSVs of segmented records, written incrementally.
"""
from __future__ import annotations

import csv
import os
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, '/workspace/altailabs-production/packages/onec-client')

from dt_parser.v3_container import (  # noqa: E402
    AmountField, AsciiField, BlobField, ControlMarker, EndMarker,
    NullMarker, Utf16Field, _tokenize_mmap,
)
from dt_parser.v3_dump import RawRecord, classify_record, _has_content  # noqa: E402

S = Path('/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')
DT = S / 'backup.dt'
OUT = S / 'export'
SENTINEL_SIZE = 1 << 62  # tokenizer loops while tell() < size; EOF breaks on empty read

# Individual fields can hold megabyte-sized text blobs (module source, document
# bodies). Cap width and count so a full pass cannot exhaust the disk.
MAX_FIELD = 150
MAX_FIELDS = 20
DISK_FLOOR = 3 * (1 << 30)   # stop extraction under 3 GB free


def _join(vals):
    out = []
    for v in vals[:MAX_FIELDS]:
        v = str(v).replace('\n', ' ').replace('\r', ' ')
        out.append(v[:MAX_FIELD])
    return ' | '.join(out)


class StreamView:
    """mmap-like sequential view over a streaming deflate decompressor.

    Supports read(n), tell(), seek(pos) for forward seeks and short rewinds
    (the tokenizer rewinds at most one byte), and size().
    """

    BACK = 64          # bytes of history kept for rewinds
    CHUNK = 8 << 20

    def __init__(self, path: Path, skip: int = 0):
        self._f = open(path, 'rb')
        self._f.seek(9)  # 8-byte magic + 1
        self._d = zlib.decompressobj(-15)
        self._buf = b''
        self._buf_start = 0   # absolute offset of _buf[0]
        self._pos = 0         # absolute logical position
        self._eof = False
        if skip:
            self._skip_forward(skip)

    def _fill(self):
        """Pull more decompressed bytes into the buffer. Returns False at EOF."""
        while not self._eof:
            comp = self._f.read(self.CHUNK)
            if not comp:
                self._eof = True
                break
            try:
                data = self._d.decompress(comp)
            except zlib.error:
                self._eof = True
                break
            if data:
                self._buf += data
                return True
            if self._d.eof:
                self._eof = True
                break
        return False

    def _trim(self):
        """Drop consumed history beyond BACK bytes."""
        keep_from = max(self._buf_start, self._pos - self.BACK)
        cut = keep_from - self._buf_start
        if cut > (1 << 20):
            self._buf = self._buf[cut:]
            self._buf_start += cut

    def _skip_forward(self, n: int):
        target = self._pos + n
        while self._pos < target:
            need = target - self._pos
            avail = len(self._buf) - (self._pos - self._buf_start)
            if avail <= 0:
                if not self._fill():
                    self._pos = target
                    return
                continue
            step = min(need, avail)
            self._pos += step
            self._trim()

    def read(self, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            i = self._pos - self._buf_start
            avail = len(self._buf) - i
            if avail <= 0:
                if not self._fill():
                    break
                continue
            step = min(n, avail)
            out += self._buf[i:i + step]
            self._pos += step
            n -= step
            self._trim()
        return bytes(out)

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int):
        if pos >= self._pos:
            self._skip_forward(pos - self._pos)
        else:
            if pos < self._buf_start:
                raise ValueError(f'rewind beyond buffered history: {pos} < {self._buf_start}')
            self._pos = pos

    def size(self) -> int:
        return SENTINEL_SIZE

    def close(self):
        self._f.close()


def segment_stream(tokens, max_fields=400):
    """Group tokens into records on END/NULL runs (streaming variant)."""
    rec = RawRecord(offset=0)
    end_run = 0
    for t in tokens:
        if isinstance(t, EndMarker):
            end_run += 1
            if end_run >= 2 and (rec.ascii_fields or rec.utf16_fields or rec.amounts):
                yield rec
                rec = RawRecord(offset=t.offset)
                end_run = 0
            continue
        end_run = 0
        if isinstance(t, Utf16Field):
            if t.value.strip():
                rec.utf16_fields.append(t.value)
                rec.field_sequence.append(('u', t.value))
        elif isinstance(t, AsciiField):
            rec.ascii_fields.append(t.value)
            rec.field_sequence.append(('a', t.value))
        elif isinstance(t, AmountField):
            rec.amounts.append(t.value)
            rec.field_sequence.append(('m', t.value))
        elif isinstance(t, NullMarker):
            rec.null_count += 1
        elif isinstance(t, BlobField):
            rec.blob_count += 1
        if len(rec.field_sequence) > max_fields:
            yield rec
            rec = RawRecord(offset=t.offset)
    if rec.ascii_fields or rec.utf16_fields or rec.amounts:
        yield rec


def main():
    skip = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 0  # 0 = no limit
    OUT.mkdir(parents=True, exist_ok=True)

    view = StreamView(DT, skip=skip)
    writers, files = {}, {}
    counts = Counter()
    n = 0

    try:
        for rec in segment_stream(_tokenize_mmap(view)):
            if not _has_content(rec):
                continue
            table = classify_record(rec)
            counts[table] += 1
            n += 1
            w = writers.get(table)
            if w is None:
                fh = open(OUT / f'{table}.csv', 'w', newline='', encoding='utf-8')
                w = csv.writer(fh)
                w.writerow(['offset', 'ascii', 'utf16', 'amounts', 'nulls', 'blobs'])
                writers[table], files[table] = w, fh
            w.writerow([
                rec.offset,
                _join(rec.ascii_fields),
                _join(rec.utf16_fields),
                _join(rec.amounts),
                rec.null_count,
                rec.blob_count,
            ])
            if n % 200000 == 0:
                avail = os.statvfs('/').f_bavail * os.statvfs('/').f_frsize
                sys.stderr.write(
                    f'PROG records={n} pos={view.tell()//(1<<20)}MB '
                    f'tables={len(counts)} avail={avail//(1<<20)}MB\n')
                sys.stderr.flush()
                if avail < DISK_FLOOR:
                    sys.stderr.write('DISK GUARD: stopping extraction\n')
                    break
                for fh in files.values():
                    fh.flush()
                with open(S / 'export_counts.txt', 'w') as cf:
                    for k, v in counts.most_common():
                        cf.write(f'{v}\t{k}\n')
            if limit and n >= limit:
                sys.stderr.write('LIMIT reached\n')
                break
    finally:
        for fh in files.values():
            fh.close()
        view.close()
        with open(S / 'export_counts.txt', 'w') as cf:
            for k, v in counts.most_common():
                cf.write(f'{v}\t{k}\n')

    sys.stderr.write(f'DONE records={n} tables={len(counts)} end_pos={view.tell()}\n')
    print(f'DONE records={n} tables={len(counts)}')


if __name__ == '__main__':
    main()

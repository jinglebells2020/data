# Streaming extraction from the .dt dump

Work in progress. These scripts read the 1C dump **without ever materialising
the database**, which is what made three in-container restore attempts fail.

## Why this path exists

`ibcmd infobase restore` writes a `1Cv8.1CD` file that reached 21.6 GB and was
still importing when the container ran out of disk. The dump's payload
decompresses to **25.72 GB** (measured, see `register_map.json`).

But the `.dt` payload is a *single raw-deflate stream* (RFC 1951, `wbits=-15`).
That means no table-of-contents to seek into - and also that it can be
decompressed **sequentially**: feed chunks in, tokenise records as they emerge,
aggregate, discard. Peak disk is the 6.2 GB source file; the 25.72 GB never
lands anywhere. Both scripts here streamed the full payload in a container with
23 GB free, and free space did not move.

Note `dt_parser/reader.py::_decompress` does *not* do this - it calls
`fh.read()` then decompresses in one shot, so it needs the whole payload in
memory. Use the streaming approach for anything full-file.

## What has been established

`locate_registers.py` sweeps the whole stream for table-name tokens and writes
`register_map.json`. It recovered **5,199 distinct objects**:

| family | objects | |
|---|---|---|
| InfoRg | 1,188 | information registers |
| Enum | 1,168 | enumerations |
| Const | 906 | constants |
| Reference | 810 | catalogues |
| Document | 518 | documents |
| AccumRg (+T/Opt/Tn) | 510 | balances and turnover |
| AccRg (+AT/CT/ED) | 9 | double-entry journal |
| other (Chrc, Node, Seq, Task, DocumentJournal) | 81 | |

The name block sits at roughly **1.958-1.984 GB** into the decompressed stream.
An earlier 120 MB window (`../dbnames.py`) landed mid-block and saw only 1,494
objects with no registers at all, which is why the registers were first thought
to be unavailable without a restore.

## The open question

The earlier extraction (`../stream_extract.py`) classified records by
pattern-matching their *contents*, so 1,957,477 of 2,050,672 records - 95.5% -
landed in an `other` bucket. Schema alone does not fix that; attribution does.

`probe_headers.py` tests the assumption attribution depends on: do table names
appear **inline as section headers** in the data region, or only in the name
block?

- **Inline** - a streaming pass can track the current table and attribute each
  record to it, typing most of that 95.5%.
- **Name block only** - the repeated tokens are reference links between
  records, attribution needs the relational layer that only a restore rebuilds,
  and `../aws-runbook/` (or `MAC_RUNBOOK.md`) is the way to proper books.

Name density is suggestive of the first (38,330 tokens for 810 catalogues, far
more than a one-per-object listing) but that is not proof. Result pending.

## Caveat that applies either way

Records recovered by streaming are reconstructed, not read from a live
infobase. There is no posting logic and no computed balances, so reconstructed
joins need checking against known figures before anyone relies on them.

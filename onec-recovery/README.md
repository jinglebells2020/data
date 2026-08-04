# 1C `.dt` recovery — streaming extraction

Tooling used to read the ANESCOM production backup
(`prod_Aneskom_KA25_backup_2026_08_02.dt`, 6.6 GB, 1C:Комплексная автоматизация 2.5)
on a host that cannot fit a full restore.

## Why this exists

The intended route is the pipeline in `altailabs-production`:

```
ibcmd infobase create --db-path=./db --restore=./backup.dt
python3 -m dt_parser.turbo_export ./db/1Cv8.1CD ./out/
```

That is the correct path and produces validated `journal.csv` / `accounts.csv` /
`counterparties.csv`. It needs disk this container does not have:

| Item | Size |
|---|---|
| Source `.dt` | 6.6 GB |
| Decompressed payload | 24 GB+ |
| Restored `1Cv8.1CD` | ≈21 GB (reached 14.4 GB before aborting) |
| Container allowance | ≈38 GB total |

`ibcmd` 8.3.21.1622 runs fine on Linux and restores correctly — only disk was missing.
**On a host with ~40 GB free, use the restore path, not this one.**

## What this does instead

`stream_extract.py` supplies a file-like adapter over a streaming zlib
decompressor exposing the `mmap` surface (`read` / `tell` / `seek` / `size`) that
`dt_parser.v3_container._tokenize_mmap` expects. The repo's tested tokenizer then
runs across the whole dump with no large intermediate written to disk.

```bash
# extract records from the data region (skip the ~1.9 GB config section)
python3 stream_extract.py 1900000000 0        # <skip_bytes> <record_limit, 0=all>

# aggregate into a findings report
python3 analyze2.py

# recover the internal table/field inventory (DBNames block)
python3 dbnames.py
```

Peak disk is the CSV output only (~1.2 GB for 2.69 M records).

## Dump layout (reverse-engineered)

| Offset | Contents |
|---|---|
| 0 | `1CIBDmpF` magic + 1 byte, then a raw deflate stream (offset 9) |
| 0 – 1.95 GB | configuration section, GUID-named entries |
| ~1.95 GB | `DBNames` — internal table/field name mapping |
| 2 GB+ | data region: `_Document957_IDRRef`, `_Fld…` (UTF-16LE, 4-byte LE length prefixes) |

Metadata objects recovered from `DBNames`: 1,494 (663 catalogues, 447 enums,
374 document types, 7 characteristic plans, 2 exchange nodes, 1 chart of accounts).
Registers were outside the sampled window, so treat that as a lower bound.

## Caveats that matter

Records are recovered by **binary pattern matching, not by reading the relational
schema**. Consequences:

- Column semantics (net / VAT / gross / quantity) are unavailable — no revenue,
  margin, or balance figure can be derived. Amounts are reported as distributions.
- Per-month counts reflect extraction yield as well as business activity, and must
  not be read as a sales trend.
- BINs and organisation names must **not** be paired: source records hold parallel,
  non-aligned lists of each. An earlier pass that zipped them produced false
  attributions and was removed.
- `product_items` mixes the company catalogue with Kazakhstan's national
  ОКЭД/ТН ВЭД classifier shipped inside 1C. `analyze2.py` separates them.
- ~0.9% of recovered text fields decode with UTF-16 byte misalignment and are
  filtered out.

Kazakh text needs the full Cyrillic block `U+0400–U+04FF`; a narrower range
(`U+0400–U+045F`) silently fragments қ / ә / ң / ө / ұ / ү / і mid-word.

## Headline operational finding

Every retail card payment is failing to sync to Bitrix24 and retrying indefinitely:

```
Required fields: paySystemId у объекта:Эквайринговая операция GTНФ-000337 от 11.02.2026
```

- 613 distinct acquiring operations observed, 0 successful syncs
- 761,395 retry log entries — one operation retried 8,216 times
- 724,645 records (~27% of everything recovered) are this one error
- Growing since January 2026 as retail card volume rises

This needs no restore to act on.

## Not committed here

Extracted records contain counterparty and employee personal data and are **not**
included in this repository. `findings_aggregate.json` holds counts and
distributions only; name lists are stripped.

# Proper analysis on the licensed 1C host — runbook

You run these on your AWS box; nothing leaves your infrastructure except a small
folder of aggregates. This gets us the audited figures the streaming extraction
could not produce: real revenue, margins, receivables and inventory.

## Before you start

Check free disk on the volume you'll work on:

```bash
df -h .
```

**You need ≥ 40 GB free.** The restore is ~21 GB of database on top of the 6.6 GB
source. This is the single thing that blocked the earlier attempt — a 1C licence
doesn't help if the volume is too small. If it's tight, attach a bigger EBS
volume and run everything there.

Also confirm the platform can read the dump:

```bash
ibcmd --version    # or: /opt/1cv8/x86_64/*/ibcmd --version
```

The dump was written by 8.3.x. Your `ibcmd` must be **the same or newer** than the
platform that produced it — an older binary will refuse the restore.

## Step 1 — restore and export

```bash
git clone https://github.com/jinglebells2020/altailabs-production
export ONEC_CLIENT=$PWD/altailabs-production/packages/onec-client

git clone https://github.com/jinglebells2020/data
cd data/onec-recovery/aws-runbook

bash 01_restore_export.sh ~/anescom
```

What it does, in order: preflight (disk, `ibcmd`, python) → re-download the 6.6 GB
`.dt` straight from the Yandex link → verify MD5 `83373a5c…` → `ibcmd infobase
create --restore` → `dt_parser.ingest` (turbo export → schema mapping →
validation).

Expect **30–90 minutes**, mostly the restore. Every stage skips if its output
already exists, so it's safe to re-run after an interruption. Logs land in
`~/anescom/restore.log` and `~/anescom/export.log`.

If it stops on `ibcmd not found`, set the path explicitly:

```bash
export IBCMD=/opt/1cv8/x86_64/8.3.24.1234/ibcmd
```

## Step 2 — aggregate

```bash
python3 02_aggregate.py ~/anescom/out ~/anescom/aggregates
```

Reads `journal.csv` in chunks (it will be large), and writes a small folder:

| File | Contents |
|---|---|
| `pl_monthly.csv` | revenue, COGS, gross profit, margin %, selling/admin expense, operating profit — per month |
| `account_group_monthly.csv` | monthly movement for cash, bank, AR, AP, inventory (raw/WIP/goods), VAT, advances |
| `counterparty_balances.csv` | receivable/payable balance per counterparty, last movement, age bucket |
| `receivables_aging.csv` | AR totals by 0-30 / 31-60 / 61-90 / 91-180 / 181-365 / 365+ |
| `largest_tables.csv` | biggest raw tables, to find the inventory registers |
| `validation_report.json` | the pipeline's own 12 checks — **the thing that makes these figures trustworthy** |
| `diagnostics.json` | columns found, row count, date range, warnings |

**Send me that folder.** It's ~100 KB.

### On personal data

Counterparties are pseudonymised by default — outputs carry `cp_3437f3`, and the
hash→name mapping is written separately as `LOCAL_ONLY_name_key.csv`. **Keep that
file; don't send it.** You can read the real names locally and I'll work from the
hashes; when a specific counterparty matters, name it in chat yourself.

If you'd rather not bother, `--include-names` skips pseudonymisation entirely —
your data, your call.

## Account mapping

Aggregation keys off the Kazakhstan standard chart of accounts:

| Group | Accounts |
|---|---|
| Revenue | 6010, 6020, 6110, 6280 |
| COGS | 7010 |
| Selling / admin / finance expense | 7110 / 7210 / 7310 |
| Cash / bank | 1010 / 1030, 1040, 1050 |
| Trade receivables / payables | 1210 / 3310 |
| Inventory — raw, WIP, goods | 1310 / 1340 / 1330 |
| VAT payable, advances | 3130, 1610, 3510 |

If ANESCOM uses a customised chart, edit `ACCOUNT_GROUPS` at the top of
`02_aggregate.py` and re-run — it's cheap, the restore is already done.

## If something breaks

The script refuses to guess. On an unrecognised journal schema it writes
`diagnostics.json` listing the columns it actually found and exits non-zero
rather than emitting a plausible-looking wrong number. Send me that file and
I'll adjust the mapping.

## Worth doing while you're on the box

The `paySystemId` failure needs no restore. On the live system, check how many
`Эквайринговая операция` documents are still queued for the Bitrix24 exchange —
613 distinct operations were visible in the backup with zero successful syncs,
and one had retried 8,216 times. Clearing the queue and populating
`paySystemId` stops roughly a quarter of all new records being error rows.

# Running the full restore on a Mac with an external drive

This works, and it's probably the least hassle of any option — the drive supplies
the disk that every other attempt lacked. Two ways to drive it: let Claude do it
for you locally, or run one Docker command yourself.

## Check the drive format FIRST

```bash
diskutil info /Volumes/YourDrive | grep -i "File System"
```

| Format | Verdict |
|---|---|
| **APFS** or **Mac OS Extended (HFS+)** | Correct — use this |
| **exFAT** | Risky: no POSIX permissions or file locking; 1C databases misbehave |
| **MS-DOS (FAT32)** | **Fatal** — 4 GB maximum file size. The database is 25 GB+ |

If the drive is FAT32 or exFAT, reformat it: **Disk Utility → Erase → APFS**.
This wipes the drive, so move anything off it first. Skipping this check is the
single most likely way to lose an hour.

Then confirm capacity — you want **≥ 60 GB free**:

```bash
df -h /Volumes/YourDrive
```

## Option A — let Claude run it (recommended)

Claude Code runs natively on macOS, and a local session has direct access to
`/Volumes/...`. In that context Claude can execute the whole pipeline itself,
which the cloud session cannot.

1. Install Claude Code on the Mac — desktop app, or `npm i -g @anthropic-ai/claude-code`
2. Open a session in a working folder
3. Point it at this runbook and the drive:

   > Run the ANESCOM 1C restore per onec-recovery/aws-runbook/MAC_RUNBOOK.md,
   > using /Volumes/YourDrive for storage, then do the P&L, receivables and
   > inventory analysis.

Everything stays on your machine, and there are no credentials to hand over.

## Option B — run it yourself

Requires **Docker Desktop**.

```bash
DRIVE=/Volumes/YourDrive                   # <- your drive
mkdir -p "$DRIVE/anescom"/{data,out,tmp}

# 1. Fetch the dump (6.6 GB) onto the drive
HREF=$(curl -sS --get \
  --data-urlencode "public_key=https://disk.yandex.com/d/SCt02VXNw6vGTw" \
  "https://cloud-api.yandex.net/v1/disk/public/resources/download" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["href"])')
curl -L --retry 5 -C - -o "$DRIVE/anescom/data/backup.dt" "$HREF"

# verify - must print 83373a5cc8a91a030af35b3a9c8fabd4
md5 -q "$DRIVE/anescom/data/backup.dt"

# 2. Build the pipeline image
git clone https://github.com/jinglebells2020/altailabs-production
cd altailabs-production/packages/onec-client
docker build --platform linux/amd64 -t onec-pipeline .

# 3. Restore + export, everything on the external drive
docker run --rm --platform linux/amd64 \
  -e TMPDIR=/work/tmp \
  -v "$DRIVE/anescom/data":/data \
  -v "$DRIVE/anescom/out":/output \
  -v "$DRIVE/anescom/tmp":/work/tmp \
  onec-pipeline /data/backup.dt /output/
```

The `-v` mounts matter: they keep the 25 GB database and ~6 GB of temp on your
external drive instead of inside Docker's VM disk image, which is much smaller
and would fill.

Then reduce it to something shareable:

```bash
cd /path/to/data/onec-recovery/aws-runbook
python3 02_aggregate.py "$DRIVE/anescom/out" "$DRIVE/anescom/aggregates"
```

Send me `aggregates` — about 100 KB, counterparties pseudonymised.

## Apple Silicon notes

`ibcmd` is a Linux x86-64 binary, so it runs under emulation on M-series Macs.
`--platform linux/amd64` is required, not optional.

Turn on **Docker Desktop → Settings → General → "Use Rosetta for x86/amd64
emulation"**. Without it the restore is several times slower. Also raise
**Settings → Resources → Memory** to 8 GB+; 1C's import is memory-hungry, and
the earlier failures showed it allocating in multi-GB bursts.

Expect **45–120 minutes** on Apple Silicon, faster on Intel.

## Sizing, measured rather than guessed

From three real attempts in a Linux container:

| Component | Size |
|---|---|
| Source `.dt`, held open throughout | 6.2 GB |
| Restored `1Cv8.1CD` | **21.6 GB and still importing** — budget 25–30 GB |
| `ibcmd` temp staging | ~6 GB |
| Exported CSVs | 5–10 GB |
| **Total** | **~45–55 GB** |

1C also pre-allocates in bursts — 5.7 GB inside one 20-second window — so leave
genuine slack rather than a tight fit.

## If it fails

`docker logs` and `$DRIVE/anescom/out/` are the places to look. The two failures
worth recognising:

- **exit 135 / SIGBUS** — the disk filled mid-write. Free space and re-run.
- **"unsupported format version"** — the 1C build in the image is older than the
  one that wrote the dump. Bump the `COPY --from=userlexey/1c ...` tag in the
  Dockerfile to a newer version and rebuild.

#!/usr/bin/env bash
# Restore the ANESCOM 1C .dt backup on a licensed 1C host and export it to CSV.
#
#   bash 01_restore_export.sh [WORKDIR]
#
# Default WORKDIR is ./anescom. Everything lands under there.
# Safe to re-run: each stage skips if its output already exists.

set -uo pipefail

WORK="${1:-$PWD/anescom}"
DT="$WORK/backup.dt"
DB="$WORK/db"
OUT="$WORK/out"
EXPECTED_MD5="83373a5cc8a91a030af35b3a9c8fabd4"
EXPECTED_SIZE=6599335145
NEED_GB=60

YANDEX_PUBLIC_URL="https://disk.yandex.com/d/SCt02VXNw6vGTw"

say(){ printf '\n\033[1m== %s\033[0m\n' "$*"; }
die(){ printf '\n\033[31mFAILED: %s\033[0m\n' "$*" >&2; exit 1; }

mkdir -p "$WORK" "$OUT"

# ── 0. Preflight ────────────────────────────────────────────────────────────
say "Preflight"

AVAIL_GB=$(df -PBG "$WORK" | awk 'NR==2{gsub(/G/,"",$4);print $4}')
echo "free disk at $WORK: ${AVAIL_GB} GB (need >= ${NEED_GB} GB)"
[ "${AVAIL_GB:-0}" -ge "$NEED_GB" ] || die "not enough free disk: ${AVAIL_GB} GB < ${NEED_GB} GB.
Measured: 6.2 GB source (held open) + 21.6 GB database still importing + ~6 GB temp.
Attach a bigger
volume or point WORKDIR at one, then re-run."

# locate ibcmd from the licensed install
IBCMD=""
for c in "$(command -v ibcmd 2>/dev/null)" \
         /opt/1cv8/x86_64/*/ibcmd /opt/1C/v8.3/x86_64/ibcmd \
         /opt/1cv8/*/ibcmd /opt/1cv8_tools/ibcmd; do
  [ -n "${c:-}" ] && [ -x "$c" ] && IBCMD="$c" && break
done
[ -n "$IBCMD" ] || die "ibcmd not found. Look for it under /opt/1cv8/ or /opt/1C/ and
set IBCMD manually, e.g.  export IBCMD=/opt/1cv8/x86_64/8.3.24.1234/ibcmd"
echo "ibcmd: $IBCMD ($("$IBCMD" --version 2>&1 | head -1))"

command -v python3 >/dev/null || die "python3 not installed"
command -v gcc     >/dev/null || echo "WARN: gcc missing - the C fast path won't build (slower, still works)"

# ── 1. Fetch the dump ───────────────────────────────────────────────────────
say "Fetch backup.dt"
if [ -f "$DT" ] && [ "$(stat -c%s "$DT")" = "$EXPECTED_SIZE" ]; then
  echo "already present, correct size - skipping download"
else
  # NOTE (2026-08-07): the original Yandex public link returns DiskNotFoundError
  # on both disk.yandex.com and disk.yandex.ru - it has been deleted or made
  # private. Place backup.dt at "$DT" yourself, or set DT_SOURCE_URL to a
  # location you control. The resolver below is kept for a replacement Yandex
  # link; it will fail cleanly against the dead one.
  if [ -n "${DT_SOURCE_URL:-}" ]; then
    echo "downloading from DT_SOURCE_URL..."
    curl -L --retry 5 --retry-delay 5 -C - -o "$DT" "$DT_SOURCE_URL" \
      || die "download from DT_SOURCE_URL failed"
  else
    echo "resolving Yandex direct link..."
    HREF=$(curl -sS --get \
        --data-urlencode "public_key=${YANDEX_PUBLIC_URL}" \
        "https://cloud-api.yandex.net/v1/disk/public/resources/download" \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["href"])') \
      || die "could not resolve the Yandex link - it is dead as of 2026-08-07.
Put backup.dt at $DT yourself, or re-run with DT_SOURCE_URL=<url>."
    echo "downloading 6.6 GB..."
    curl -L --retry 5 --retry-delay 5 -C - -o "$DT" "$HREF" || die "download failed"
  fi
fi

echo "verifying checksum..."
GOT_MD5=$(md5sum "$DT" | cut -d' ' -f1)
[ "$GOT_MD5" = "$EXPECTED_MD5" ] || die "MD5 mismatch: got $GOT_MD5, expected $EXPECTED_MD5"
echo "MD5 OK: $GOT_MD5"

# ── 2. Restore .dt -> file infobase ─────────────────────────────────────────
say "Restore .dt -> 1Cv8.1CD  (this is the long step, 10-40 min)"
if [ -f "$DB/1Cv8.1CD" ] && [ "$(stat -c%s "$DB/1Cv8.1CD")" -gt 1000000000 ]; then
  echo "database already restored ($(du -h "$DB/1Cv8.1CD" | cut -f1)) - skipping"
else
  mkdir -p "$DB"
  "$IBCMD" infobase create --db-path="$DB" --restore="$DT" 2>&1 | tee "$WORK/restore.log"
  [ -f "$DB/1Cv8.1CD" ] || die "restore produced no 1Cv8.1CD - see $WORK/restore.log"
fi
echo "restored: $(du -h "$DB/1Cv8.1CD" | cut -f1)"

# ── 3. Export to CSV ────────────────────────────────────────────────────────
say "Export tables -> CSV"
[ -n "${ONEC_CLIENT:-}" ] || ONEC_CLIENT="$(cd "$(dirname "$0")/../.." 2>/dev/null && pwd)/packages/onec-client"
[ -d "$ONEC_CLIENT/dt_parser" ] || die "dt_parser not found.
Clone the pipeline first:
  git clone https://github.com/jinglebells2020/altailabs-production
  export ONEC_CLIENT=\$PWD/altailabs-production/packages/onec-client
then re-run this script."

cd "$ONEC_CLIENT" || die "cannot enter $ONEC_CLIENT"
python3 -m pip install -q "pandas>=2.0" "onec_dtools>=0.5.0" "lxml>=4.9" 2>&1 | tail -1
[ -f dt_parser/fast_decode.so ] || gcc -O3 -shared -fPIC -o dt_parser/fast_decode.so dt_parser/fast_decode.c 2>/dev/null \
  && echo "C fast path ready"

# ingest() runs: turbo export -> schema map -> validation
python3 -m dt_parser.ingest "$DB/1Cv8.1CD" "$OUT" 2>&1 | tee "$WORK/export.log"

say "Done"
echo "outputs in $OUT:"
ls -la "$OUT" | head -20
cat <<EOF

Next:
  python3 02_aggregate.py "$OUT" "$WORK/aggregates"

Then send back the contents of $WORK/aggregates (small, ~100 KB, no personal data).
EOF

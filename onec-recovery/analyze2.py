#!/usr/bin/env python3
"""Defensible business analytics over records extracted from the ANESCOM 1C dump.

Design rule: separate DIRECTLY VERIFIABLE facts (document identifiers with
embedded dates, BINs, names, product lines) from INFERRED quantities (amount
semantics). Anything that cannot be established from the extracted structure
is reported as unknown rather than estimated.

Corrections over the first pass:
  * BIN <-> name pairing dropped. Records hold parallel, non-aligned lists of
    BINs and names, so pairing them produced false attributions.
  * Product catalogue split: national OKED/TNVED classifier reference data
    (shipped inside 1C) is separated from ANESCOM's own nomenclature, which
    lives in document/delivery lines.
  * No summing of unlabelled amounts as "revenue". Only document line amounts
    are treated as monetary, and reported as distributions.
"""
from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

csv.field_size_limit(10 ** 9)

S = Path('/tmp/claude-0/-home-user-data/fb795424-174e-5578-beb7-6b39c05c3ffe/scratchpad')
EX = S / 'export'
OUT = S / 'analysis'
OUT.mkdir(exist_ok=True)

OWN_BIN = '100340008066'

ESF_RE = re.compile(r'ESF-(\d{12})-(\d{8})-(\d+)')
SNT_RE = re.compile(r'KZ-SNT-(\d{4})-(\d{12})-(\d{8})-(\d+)')
DOCREF_RE = re.compile(r'\b\d{4}(\d{2})(\d{2})(\d{4})N\d+\b')
BIN_RE = re.compile(r'^\d{12}$')
AMOUNT_RE = re.compile(r'^-?\d+(\.\d+)?$')
OKED_RE = re.compile(r'^\d{2}\.\d{2}')

ORG_MARK = ('ТОО', 'Товарищество', 'АО ', 'ЖШС', 'ИП ', 'ООО', 'ГКП', 'ГУ ', 'ТДО')


def is_garbled(s: str) -> bool:
    if not s:
        return True
    bad = sum(1 for c in s if '　' <= c <= '鿿' or '' <= c <= '')
    return bad > len(s) * 0.15


def rows(name):
    p = EX / f'{name}.csv'
    if not p.exists():
        return
    with open(p, newline='', encoding='utf-8', errors='replace') as fh:
        for r in csv.DictReader(fh):
            yield r


def parts(r, col):
    return [v.strip() for v in (r.get(col) or '').split(' | ') if v.strip()]


def ymd8(s):
    try:
        y, m, d = int(s[:4]), int(s[4:6]), int(s[6:8])
        if 2000 <= y <= 2030 and 1 <= m <= 12 and 1 <= d <= 31:
            return date(y, m, d)
    except Exception:
        pass
    return None


R = {}

# ── Documents with embedded dates (verifiable) ──────────────────────────────
esf, snt = {}, {}
esf_partner_bins, snt_partner_bins = Counter(), Counter()

for r in rows('esf_invoices'):
    for bin_, d8, seq in ESF_RE.findall(r.get('ascii', '')):
        dt = ymd8(d8)
        if dt:
            esf[f'{bin_}-{d8}-{seq}'] = (dt, bin_)
            if bin_ != OWN_BIN:
                esf_partner_bins[bin_] += 1

for src in ('delivery_docs', 'document_entries', 'other', 'text_records'):
    for r in rows(src):
        blob = r.get('ascii', '')
        if 'KZ-SNT-' not in blob:
            continue
        for reg, bin_, d8, seq in SNT_RE.findall(blob):
            dt = ymd8(d8)
            if dt:
                snt[f'{bin_}-{d8}-{seq}'] = (dt, bin_)
                if bin_ != OWN_BIN:
                    snt_partner_bins[bin_] += 1

def by_period(d, fmt='%Y-%m'):
    c = Counter()
    for dt, _ in d.values():
        c[dt.strftime(fmt)] += 1
    return dict(sorted(c.items()))

R['documents_with_dates'] = {
    'esf_invoices': {
        'unique': len(esf),
        'date_range': [min(v[0] for v in esf.values()).isoformat(),
                       max(v[0] for v in esf.values()).isoformat()] if esf else None,
        'by_year': by_period(esf, '%Y'),
        'by_month': by_period(esf),
        'distinct_partner_bins': len(esf_partner_bins),
    },
    'snt_delivery_notes': {
        'unique': len(snt),
        'date_range': [min(v[0] for v in snt.values()).isoformat(),
                       max(v[0] for v in snt.values()).isoformat()] if snt else None,
        'by_year': by_period(snt, '%Y'),
        'distinct_partner_bins': len(snt_partner_bins),
    },
}

# ── Document lines: dates + monetary amounts ────────────────────────────────
line_by_month = Counter()
line_amounts = []
line_amt_by_month = defaultdict(list)
nomenclature = Counter()

for src in ('document_entries', 'delivery_docs'):
    for r in rows(src):
        m = DOCREF_RE.search(r.get('ascii', ''))
        dt = None
        if m:
            try:
                d_, mo_, y_ = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if 2000 <= y_ <= 2030 and 1 <= mo_ <= 12 and 1 <= d_ <= 31:
                    dt = date(y_, mo_, d_)
            except Exception:
                dt = None
        amts = [float(a) for a in parts(r, 'amounts') if AMOUNT_RE.match(a)]
        for nm in parts(r, 'utf16'):
            if not is_garbled(nm) and 3 < len(nm) < 120:
                nomenclature[nm] += 1
        if amts:
            line_amounts.extend(amts)
        if dt:
            line_by_month[dt.strftime('%Y-%m')] += 1
            if amts:
                line_amt_by_month[dt.strftime('%Y-%m')].append(max(amts))


def dist(vals):
    if not vals:
        return {}
    sv = sorted(vals)
    n = len(sv)
    return {
        'count': n,
        'min': round(sv[0], 2), 'max': round(sv[-1], 2),
        'median': round(sv[n // 2], 2),
        'p25': round(sv[n // 4], 2), 'p75': round(sv[3 * n // 4], 2),
        'p95': round(sv[int(n * 0.95)], 2),
        'negatives': sum(1 for v in sv if v < 0),
        'sum': round(sum(sv), 2),
    }


R['document_lines'] = {
    'dated_lines': sum(line_by_month.values()),
    'by_month': dict(sorted(line_by_month.items())),
    'line_amount_distribution_KZT': dist(line_amounts),
    'note': 'Amounts are per-line values parsed from document records; column '
            'semantics (net / VAT / gross / qty) are not recoverable without '
            'the table schema, so these are distributions, not revenue.',
}

# ── Counterparties (counts only; no BIN<->name pairing) ─────────────────────
all_bins, org_names = set(), Counter()
for src in ('counterparties', 'iin_refs', 'esf_invoices', 'account_entries'):
    for r in rows(src):
        for v in parts(r, 'ascii'):
            if BIN_RE.match(v) and v != OWN_BIN:
                all_bins.add(v)
        for nm in parts(r, 'utf16'):
            if is_garbled(nm) or not (3 < len(nm) < 120):
                continue
            if any(k in nm for k in ORG_MARK):
                org_names[nm] += 1

forms = Counter()
for nm in org_names:
    if 'ТОО' in nm or 'Товарищество' in nm or 'ЖШС' in nm:
        forms['ТОО / ЖШС (LLP)'] += 1
    elif nm.startswith('ИП ') or ' ИП ' in nm:
        forms['ИП (sole trader)'] += 1
    elif 'АО' in nm:
        forms['АО (JSC)'] += 1
    elif 'ГКП' in nm or 'ГУ ' in nm:
        forms['ГКП / ГУ (state entity)'] += 1
    elif 'ООО' in nm:
        forms['ООО (foreign LLC)'] += 1

R['counterparties'] = {
    'distinct_bins_excluding_own': len(all_bins),
    'distinct_org_names': len(org_names),
    'legal_form_mix': dict(forms.most_common()),
    'most_frequent_names': org_names.most_common(25),
    'note': 'BIN and name are NOT paired: source records contain parallel, '
            'non-aligned lists of identifiers and names.',
}

# ── Product nomenclature vs national classifier ─────────────────────────────
classifier_names, classifier_codes = Counter(), set()
for r in rows('product_items'):
    codes = [v for v in parts(r, 'ascii') if OKED_RE.match(v)]
    classifier_codes.update(codes)
    for nm in parts(r, 'utf16'):
        if not is_garbled(nm) and 3 < len(nm) < 150:
            classifier_names[nm] += 1

fam = Counter()
for nm in nomenclature:
    w = re.split(r'[\s,]+', nm.strip())[0]
    if len(w) > 2:
        fam[w] += 1

R['products'] = {
    'anescom_nomenclature_lines': {
        'distinct_items': len(nomenclature),
        'top_items': nomenclature.most_common(30),
        'top_families': fam.most_common(20),
    },
    'national_classifier_reference': {
        'distinct_codes': len(classifier_codes),
        'distinct_entries': len(classifier_names),
        'note': 'OKED/TNVED reference data shipped inside the 1C configuration '
                '- NOT the company product catalogue.',
    },
}

# ── Mine the unclassified bucket ────────────────────────────────────────────
# 94% of records land in `other`: the classifier keys off ASCII patterns, but
# many real records carry their identity in Cyrillic text instead (dated
# operations, document series, nomenclature). Mine those directly.
RU_DATE_RE = re.compile(r'от (\d{2})\.(\d{2})\.(20\d{2})')
DOCNUM_RE = re.compile(r'\b([А-ЯЁA-Z]{2,8})[-–—](\d{5,6})\b')
DOCTYPE_RE = re.compile(r'^([А-ЯЁ][а-яё]+(?: [а-яё]+){0,3})\s+[А-ЯЁA-Z]{2,8}[-–—]\d{5,6}')

other_dates = Counter()
doc_series = Counter()
doc_types = Counter()
other_named = Counter()

for r in rows('other'):
    u = r.get('utf16', '')
    if not u:
        continue
    for dd, mm, yy in RU_DATE_RE.findall(u):
        try:
            y, m_, d_ = int(yy), int(mm), int(dd)
            if 2000 <= y <= 2030 and 1 <= m_ <= 12 and 1 <= d_ <= 31:
                other_dates[f'{y:04d}-{m_:02d}'] += 1
        except Exception:
            pass
    for pref, num in DOCNUM_RE.findall(u):
        doc_series[pref] += 1
    for seg in u.split(' | '):
        seg = seg.strip()
        if not seg or is_garbled(seg):
            continue
        m = DOCTYPE_RE.match(seg)
        if m:
            doc_types[m.group(1)] += 1
        elif 4 < len(seg) < 70 and re.match(r'^[А-ЯЁA-Z]', seg) and not seg.startswith('http'):
            other_named[seg] += 1

R['unclassified_mining'] = {
    'dated_mentions_by_month': dict(sorted(other_dates.items())),
    'dated_mentions_total': sum(other_dates.values()),
    'document_number_series': doc_series.most_common(25),
    'document_types': doc_types.most_common(25),
    'frequent_text_entities': other_named.most_common(40),
    'note': 'Recovered from records the pattern classifier could not type. '
            'Dates here are mentions inside document descriptions, so they '
            'indicate activity periods, not a document count.',
}

# ── Coverage & quality ──────────────────────────────────────────────────────
tbl_counts = {}
for p in sorted(EX.glob('*.csv')):
    with open(p, 'rb') as fh:
        tbl_counts[p.stem] = max(0, sum(1 for _ in fh) - 1)

clean = garb = 0
for src in ('document_entries', 'counterparties', 'product_items', 'text_records'):
    for r in rows(src):
        for v in parts(r, 'utf16'):
            if is_garbled(v):
                garb += 1
            else:
                clean += 1

R['coverage'] = {
    'records_by_table': tbl_counts,
    'total_records': sum(tbl_counts.values()),
    'text_fields_clean': clean,
    'text_fields_garbled': garb,
    'garbled_pct': round(100 * garb / max(1, clean + garb), 2),
    'caveat': 'Records are recovered by binary pattern matching, not by reading '
              'the relational schema. Coverage is partial and uneven across '
              'periods, so counts per month reflect extraction yield as well as '
              'business activity and must not be read as a sales trend.',
}

with open(OUT / 'report2.json', 'w') as fh:
    json.dump(R, fh, ensure_ascii=False, indent=1)
print('written', OUT / 'report2.json')
for k in R:
    print(' -', k)

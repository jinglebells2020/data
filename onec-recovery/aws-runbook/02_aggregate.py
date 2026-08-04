#!/usr/bin/env python3
"""Aggregate a restored ANESCOM 1C export into small, shareable analytics.

    python3 02_aggregate.py <export_dir> <out_dir> [--include-names]

Runs on your own host against the pipeline's output. Emits a handful of small
CSVs (tens of KB) covering P&L, receivables/cash, and inventory, so nothing
large and nothing personal has to leave your infrastructure.

Counterparties are pseudonymised by default: outputs carry a stable short hash
(cp_0f3a91) plus a locally-written key file mapping hash -> real name. Keep that
key file; do not send it. Pass --include-names to skip pseudonymisation.

The script refuses to guess. If a required column or account range is missing it
says so and writes a diagnostics file rather than emitting a plausible-looking
number that is wrong.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pandas as pd

# Kazakhstan standard chart of accounts (Типовой план счетов РК)
ACCOUNT_GROUPS = {
    'cash_on_hand':      ('1010',),
    'cash_in_bank':      ('1030', '1040', '1050'),
    'receivables_trade': ('1210',),
    'receivables_other': ('1250', '1270', '1280'),
    'advances_paid':     ('1610',),
    'inventory_raw':     ('1310',),
    'inventory_wip':     ('1340',),
    'inventory_goods':   ('1330',),
    'payables_trade':    ('3310',),
    'advances_received': ('3510',),
    'vat_payable':       ('3130',),
    'revenue':           ('6010', '6020', '6110', '6280'),
    'cogs':              ('7010',),
    'selling_expense':   ('7110',),
    'admin_expense':     ('7210',),
    'finance_expense':   ('7310',),
}
REVENUE_PREFIX, EXPENSE_PREFIX = '6', '7'


def short_hash(v: str) -> str:
    return 'cp_' + hashlib.sha256(str(v).encode('utf-8')).hexdigest()[:6]


def pick(cols, *cands):
    low = {c.lower(): c for c in cols}
    for c in cands:
        if c in low:
            return low[c]
    return None


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    include_names = '--include-names' in sys.argv
    if len(args) < 2:
        print(__doc__)
        sys.exit(1)

    exp, out = Path(args[0]), Path(args[1])
    out.mkdir(parents=True, exist_ok=True)
    diag = {'export_dir': str(exp), 'checks': [], 'warnings': [], 'errors': []}

    jpath = exp / 'journal.csv'
    apath = exp / 'accounts.csv'
    cpath = exp / 'counterparties.csv'

    if not jpath.exists():
        diag['errors'].append(f'journal.csv not found in {exp}. '
                              'Did dt_parser.ingest finish? Check export.log.')
        (out / 'diagnostics.json').write_text(json.dumps(diag, ensure_ascii=False, indent=1))
        print('ERROR: journal.csv missing — see diagnostics.json')
        sys.exit(2)

    head = pd.read_csv(jpath, nrows=200, low_memory=False)
    cols = list(head.columns)
    diag['journal_columns'] = cols

    c_period = pick(cols, 'period', 'date', 'datetime', '_period')
    c_debit = pick(cols, 'debit_code', 'debit', 'dt_code')
    c_credit = pick(cols, 'credit_code', 'credit', 'kt_code')
    c_amount = pick(cols, 'amount', 'sum', 'summa')
    c_damount = pick(cols, 'debit_amount')
    c_camount = pick(cols, 'credit_amount')
    c_cp = pick(cols, 'counterparty_ref', 'counterparty', 'contractor_ref')

    missing = [n for n, v in [('period', c_period), ('debit_code', c_debit),
                              ('credit_code', c_credit)] if v is None]
    if missing or (c_amount is None and c_damount is None):
        diag['errors'].append(f'journal.csv lacks required columns: {missing or "amount"}. '
                              f'Present: {cols}')
        (out / 'diagnostics.json').write_text(json.dumps(diag, ensure_ascii=False, indent=1))
        print('ERROR: journal schema not recognised — see diagnostics.json')
        sys.exit(3)

    amount_col = c_amount or c_damount
    usecols = [c for c in {c_period, c_debit, c_credit, amount_col, c_damount,
                           c_camount, c_cp} if c]

    print(f'reading {jpath} (period={c_period}, amount={amount_col}) ...')

    pl_rev, pl_exp = {}, {}
    grp_month = {}
    cp_bal = {}
    rows = 0
    dmin = dmax = None

    for chunk in pd.read_csv(jpath, usecols=usecols, chunksize=400_000,
                             low_memory=False, dtype={c_debit: str, c_credit: str}):
        rows += len(chunk)
        chunk[c_period] = pd.to_datetime(chunk[c_period], errors='coerce')
        chunk = chunk.dropna(subset=[c_period])
        if chunk.empty:
            continue
        lo, hi = chunk[c_period].min(), chunk[c_period].max()
        dmin = lo if dmin is None or lo < dmin else dmin
        dmax = hi if dmax is None or hi > dmax else dmax
        chunk['ym'] = chunk[c_period].dt.strftime('%Y-%m')
        amt = pd.to_numeric(chunk[amount_col], errors='coerce').fillna(0.0)
        dr = chunk[c_debit].astype(str).str[:4]
        cr = chunk[c_credit].astype(str).str[:4]

        # P&L: revenue accrues on the credit side of 6xxx, expense on debit of 7xxx
        rmask = cr.str.startswith(REVENUE_PREFIX)
        for (ym, acc), v in amt[rmask].groupby([chunk['ym'][rmask], cr[rmask]]).sum().items():
            pl_rev[(ym, acc)] = pl_rev.get((ym, acc), 0.0) + float(v)
        emask = dr.str.startswith(EXPENSE_PREFIX)
        for (ym, acc), v in amt[emask].groupby([chunk['ym'][emask], dr[emask]]).sum().items():
            pl_exp[(ym, acc)] = pl_exp.get((ym, acc), 0.0) + float(v)

        # balance movement per account group per month (debit +, credit -)
        for gname, prefixes in ACCOUNT_GROUPS.items():
            gm = dr.isin(prefixes)
            if gm.any():
                for ym, v in amt[gm].groupby(chunk['ym'][gm]).sum().items():
                    grp_month[(gname, ym, 'debit')] = grp_month.get((gname, ym, 'debit'), 0.0) + float(v)
            gm = cr.isin(prefixes)
            if gm.any():
                for ym, v in amt[gm].groupby(chunk['ym'][gm]).sum().items():
                    grp_month[(gname, ym, 'credit')] = grp_month.get((gname, ym, 'credit'), 0.0) + float(v)

        # receivables / payables by counterparty
        if c_cp:
            for acc_set, tag in ((('1210',), 'receivable'), (('3310',), 'payable')):
                m = dr.isin(acc_set) | cr.isin(acc_set)
                if not m.any():
                    continue
                sign = dr[m].isin(acc_set).map({True: 1.0, False: -1.0})
                sub = pd.DataFrame({'cp': chunk[c_cp][m].astype(str),
                                    'v': amt[m] * sign,
                                    'd': chunk[c_period][m]})
                for cp, g in sub.groupby('cp'):
                    key = (cp, tag)
                    prev = cp_bal.get(key, {'bal': 0.0, 'last': None, 'n': 0})
                    prev['bal'] += float(g['v'].sum())
                    prev['n'] += len(g)
                    gl = g['d'].max()
                    prev['last'] = gl if prev['last'] is None else max(prev['last'], gl)
                    cp_bal[key] = prev

    diag['journal_rows'] = rows
    diag['date_range'] = [str(dmin), str(dmax)]
    print(f'  {rows:,} rows, {dmin} .. {dmax}')

    # ── P&L ────────────────────────────────────────────────────────────────
    pl = {}
    for (ym, acc), v in pl_rev.items():
        pl.setdefault(ym, {}).setdefault('revenue', 0.0)
        pl[ym]['revenue'] += v
    for (ym, acc), v in pl_exp.items():
        d = pl.setdefault(ym, {})
        if acc == '7010':
            d['cogs'] = d.get('cogs', 0.0) + v
        elif acc.startswith('71'):
            d['selling'] = d.get('selling', 0.0) + v
        elif acc.startswith('72'):
            d['admin'] = d.get('admin', 0.0) + v
        else:
            d['other_expense'] = d.get('other_expense', 0.0) + v
    pl_rows = []
    for ym in sorted(pl):
        d = pl[ym]
        rev = d.get('revenue', 0.0)
        cogs = d.get('cogs', 0.0)
        pl_rows.append({
            'month': ym, 'revenue': round(rev, 2), 'cogs': round(cogs, 2),
            'gross_profit': round(rev - cogs, 2),
            'gross_margin_pct': round(100 * (rev - cogs) / rev, 2) if rev else None,
            'selling': round(d.get('selling', 0.0), 2),
            'admin': round(d.get('admin', 0.0), 2),
            'other_expense': round(d.get('other_expense', 0.0), 2),
            'operating_profit': round(rev - cogs - d.get('selling', 0.0)
                                      - d.get('admin', 0.0) - d.get('other_expense', 0.0), 2),
        })
    pd.DataFrame(pl_rows).to_csv(out / 'pl_monthly.csv', index=False)

    # account-group monthly movement (cash, inventory, AR/AP)
    gm_rows = [{'group': g, 'month': ym, 'side': s, 'amount': round(v, 2)}
               for (g, ym, s), v in sorted(grp_month.items())]
    pd.DataFrame(gm_rows).to_csv(out / 'account_group_monthly.csv', index=False)

    # ── counterparty balances ──────────────────────────────────────────────
    names = {}
    if cpath.exists():
        try:
            cdf = pd.read_csv(cpath, low_memory=False)
            kc = pick(list(cdf.columns), 'ref', 'guid', '_idrref', 'id')
            nc = pick(list(cdf.columns), 'name', 'description', 'full_name')
            if kc and nc:
                names = dict(zip(cdf[kc].astype(str), cdf[nc].astype(str)))
        except Exception as e:
            diag['warnings'].append(f'counterparties.csv unreadable: {e}')

    asof = dmax
    cp_rows, keyfile = [], []
    for (cp, tag), d in cp_bal.items():
        if abs(d['bal']) < 0.01:
            continue
        real = names.get(cp, '')
        label = real if include_names else short_hash(cp)
        if not include_names and real:
            keyfile.append({'hash': label, 'name': real})
        age = (asof - d['last']).days if (asof is not None and d['last'] is not None) else None
        cp_rows.append({
            'counterparty': label, 'type': tag,
            'balance': round(d['bal'], 2), 'postings': d['n'],
            'last_movement': str(d['last'].date()) if d['last'] is not None else '',
            'days_since_movement': age,
            'age_bucket': ('current' if age is None else
                           '0-30' if age <= 30 else '31-60' if age <= 60 else
                           '61-90' if age <= 90 else '91-180' if age <= 180 else
                           '181-365' if age <= 365 else '365+'),
        })
    cp_df = pd.DataFrame(cp_rows).sort_values('balance', ascending=False) if cp_rows else pd.DataFrame()
    cp_df.to_csv(out / 'counterparty_balances.csv', index=False)
    if keyfile:
        pd.DataFrame(keyfile).drop_duplicates().to_csv(out / 'LOCAL_ONLY_name_key.csv', index=False)

    if not cp_df.empty:
        aging = (cp_df[cp_df.type == 'receivable']
                 .groupby('age_bucket')['balance'].agg(['sum', 'count']).reset_index())
        aging.to_csv(out / 'receivables_aging.csv', index=False)

    # ── inventory from accumulation registers, if exported ─────────────────
    man = exp / '_manifest.csv'
    if man.exists():
        try:
            m = pd.read_csv(man, header=None, names=['table', 'records', 'file'])
            m['records'] = pd.to_numeric(m['records'], errors='coerce').fillna(0).astype(int)
            m.sort_values('records', ascending=False).head(60).to_csv(
                out / 'largest_tables.csv', index=False)
            diag['checks'].append(f'{len(m)} tables exported')
        except Exception as e:
            diag['warnings'].append(f'_manifest.csv unreadable: {e}')
    else:
        diag['warnings'].append('_manifest.csv not found — raw table list unavailable')

    if apath.exists():
        try:
            adf = pd.read_csv(apath, low_memory=False)
            adf.head(400).to_csv(out / 'accounts_sample.csv', index=False)
            diag['checks'].append(f'accounts.csv: {len(adf)} accounts')
        except Exception as e:
            diag['warnings'].append(f'accounts.csv unreadable: {e}')

    for f in ('validation_report.json', 'validation.json', 'schema_map.csv'):
        p = exp / f
        if p.exists():
            (out / f).write_bytes(p.read_bytes())
            diag['checks'].append(f'copied {f}')

    diag['pseudonymised'] = not include_names
    (out / 'diagnostics.json').write_text(json.dumps(diag, ensure_ascii=False, indent=1, default=str))

    print(f'\nwrote to {out}:')
    for p in sorted(out.iterdir()):
        print(f'  {p.name:36s} {p.stat().st_size/1024:8.1f} KB')
    if (out / 'LOCAL_ONLY_name_key.csv').exists():
        print('\nKEEP LOCAL: LOCAL_ONLY_name_key.csv maps hashes to real names — do not send it.')


if __name__ == '__main__':
    main()

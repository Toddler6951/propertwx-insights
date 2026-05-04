#!/usr/bin/env python3
"""build_insights.py — Generate insights JSON from per-state SED CSVs.

Reads the per-state per-year CSVs produced by fetch_sed.py (in the
storm-claim-tool repo) and emits:

  data/insights.json            — top-level analytics (used by the landing page)
  data/by-state/<STATE>.json    — per-state deep dives (used by per-state pages)

Usage:
  python build_insights.py [--src path/to/sed-data] [--out data]

When run from the GitHub Action, --src points at the checked-out
storm-claim-tool/sed-data folder and --out is the local data/ folder.
"""

import argparse
import csv
import datetime as dt
import gzip
import io
import json
import os
import re
import sys
from collections import defaultdict, Counter
from pathlib import Path


STATES = [
    'AL','AK','AZ','AR','CA','CO','CT','DE','DC','FL','GA','HI','ID','IL',
    'IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE',
    'NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','PR','RI','SC',
    'SD','TN','TX','UT','VT','VA','WA','WV','WI','WY',
]

EVENT_TO_CAT = {
    'Hail': 'Hail',
    'Thunderstorm Wind': 'Wind',
    'High Wind': 'Wind',
    'Strong Wind': 'Wind',
    'Marine Thunderstorm Wind': 'Wind',
    'Tornado': 'Tornado',
    'Funnel Cloud': 'Tornado',
    'Flash Flood': 'Flood',
    'Flood': 'Flood',
    'Coastal Flood': 'Flood',
    'Lakeshore Flood': 'Flood',
    'Lightning': 'Lightning',
    'Heavy Snow': 'Winter',
    'Winter Storm': 'Winter',
    'Winter Weather': 'Winter',
    'Ice Storm': 'Winter',
    'Blizzard': 'Winter',
    'Sleet': 'Winter',
    'Heavy Rain': 'Flood',
    'Tropical Storm': 'Wind',
    'Hurricane': 'Wind',
    'Tropical Depression': 'Wind',
}


def parse_sed_date(s):
    """Parse 'DD-MMM-YY HH:MM:SS' to datetime, or None."""
    if not s:
        return None
    m = re.match(r'^(\d{1,2})-([A-Za-z]{3})-(\d{2,4})\s+(\d{1,2}):(\d{2}):(\d{2})$', s)
    if not m:
        return None
    months = {'JAN':1,'FEB':2,'MAR':3,'APR':4,'MAY':5,'JUN':6,
              'JUL':7,'AUG':8,'SEP':9,'OCT':10,'NOV':11,'DEC':12}
    day = int(m.group(1))
    mo = months.get(m.group(2).upper())
    if not mo:
        return None
    yr = int(m.group(3))
    if yr < 100:
        yr += 2000 if yr < 50 else 1900
    return dt.datetime(yr, mo, day, int(m.group(4)), int(m.group(5)), int(m.group(6)))


def categorize(event_type):
    if not event_type:
        return 'Other'
    cat = EVENT_TO_CAT.get(event_type)
    if cat:
        return cat
    t = event_type.upper()
    if 'HAIL' in t: return 'Hail'
    if 'TORNADO' in t or 'FUNNEL' in t: return 'Tornado'
    if 'WIND' in t or 'TSTM' in t or 'THUNDERSTORM' in t: return 'Wind'
    if 'FLOOD' in t or 'FLD' in t: return 'Flood'
    if 'LIGHTNING' in t or 'LTG' in t: return 'Lightning'
    if any(w in t for w in ('SNOW','ICE','BLIZZARD','FREEZ','WINTER','SLEET')):
        return 'Winter'
    return 'Other'


def safe_float(s):
    try:
        return float(s) if s not in (None, '', ' ') else None
    except (ValueError, TypeError):
        return None


def stream_state_year_rows(src_dir, state, year):
    """Yield event dicts from one state-year CSV. Skips rows without a parseable date."""
    p = Path(src_dir) / state / f'details_{year}.csv'
    if not p.exists():
        return
    with p.open('r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for r in reader:
            d = parse_sed_date(r.get('BEGIN_DATE_TIME', ''))
            if d is None:
                continue
            yield {
                'date': d,
                'event_type': r.get('EVENT_TYPE', ''),
                'category': categorize(r.get('EVENT_TYPE', '')),
                'magnitude': safe_float(r.get('MAGNITUDE')),
                'state': state,
                'cz_name': r.get('CZ_NAME', '').strip(),
                'lat': safe_float(r.get('BEGIN_LAT')),
                'lon': safe_float(r.get('BEGIN_LON')),
                'tor_scale': r.get('TOR_F_SCALE', '').strip(),
            }


def build(src_dir, out_dir):
    src = Path(src_dir)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / 'by-state').mkdir(exist_ok=True)

    available_years = sorted({
        int(yf.stem.split('_')[1])
        for st in STATES
        for yf in (src / st).glob('details_*.csv')
        if (src / st).is_dir()
    })
    if not available_years:
        raise SystemExit(f'No SED CSVs found under {src!r}')

    print(f'Years available: {min(available_years)}–{max(available_years)} ({len(available_years)} total)')

    # Top-level aggregates
    top_by_year = defaultdict(int)              # year -> total events
    top_by_year_cat = defaultdict(lambda: Counter())  # year -> Counter(cat)
    top_by_state_recent = Counter()             # state -> count (last 10y)
    top_by_state_all = Counter()                # state -> count (all years)
    top_by_month = Counter()                    # month -> count (1..12)
    top_hail = []                               # list of (mag, date, state, county) descending
    top_tornadoes = []                          # list of EF events
    top_wind = []
    grand_totals = Counter()                    # category counts
    cutoff_recent = max(available_years) - 9    # last 10 yrs

    # Per-state aggregates
    state_data = {st: {
        'by_year': defaultdict(int),
        'by_year_cat': defaultdict(lambda: Counter()),
        'by_month': Counter(),
        'totals': Counter(),
        'top_hail': [],
        'top_tornadoes': [],
        'top_wind': [],
        'top_counties': Counter(),
    } for st in STATES}

    total_rows = 0
    for state in STATES:
        if not (src / state).is_dir():
            continue
        for year in available_years:
            for ev in stream_state_year_rows(src, state, year):
                total_rows += 1
                cat = ev['category']
                yr = ev['date'].year
                mo = ev['date'].month
                # global
                top_by_year[yr] += 1
                top_by_year_cat[yr][cat] += 1
                grand_totals[cat] += 1
                top_by_month[mo] += 1
                top_by_state_all[state] += 1
                if yr >= cutoff_recent:
                    top_by_state_recent[state] += 1
                # state
                sd = state_data[state]
                sd['by_year'][yr] += 1
                sd['by_year_cat'][yr][cat] += 1
                sd['by_month'][mo] += 1
                sd['totals'][cat] += 1
                if ev['cz_name']:
                    sd['top_counties'][ev['cz_name']] += 1
                # top events (only keep ones with magnitude or relevant)
                if cat == 'Hail' and ev['magnitude'] is not None:
                    rec = (ev['magnitude'], ev['date'].isoformat(), state, ev['cz_name'], ev['lat'], ev['lon'])
                    top_hail.append(rec)
                    sd['top_hail'].append(rec)
                elif cat == 'Tornado' and ev['tor_scale']:
                    # parse "EF3" -> 3, fallback -1
                    m = re.search(r'\d', ev['tor_scale'])
                    rank = int(m.group(0)) if m else -1
                    rec = (rank, ev['tor_scale'], ev['date'].isoformat(), state, ev['cz_name'], ev['lat'], ev['lon'])
                    top_tornadoes.append(rec)
                    sd['top_tornadoes'].append(rec)
                elif cat == 'Wind' and ev['magnitude'] is not None:
                    rec = (ev['magnitude'], ev['date'].isoformat(), state, ev['cz_name'], ev['lat'], ev['lon'])
                    top_wind.append(rec)
                    sd['top_wind'].append(rec)
        print(f'  {state}: {sum(state_data[state]["totals"].values())} events')

    # Sort top events and trim to top 25.
    # Sort key is just the leading magnitude/rank — sorting whole tuples breaks
    # when later fields (cz_name, lat, lon) contain None.
    top_hail.sort(key=lambda r: r[0], reverse=True); top_hail = top_hail[:25]
    top_tornadoes.sort(key=lambda r: r[0], reverse=True); top_tornadoes = top_tornadoes[:25]
    top_wind.sort(key=lambda r: r[0], reverse=True); top_wind = top_wind[:25]

    # Year trend: linear slope (events per year over time, by category)
    sorted_years = sorted(top_by_year.keys())
    def slope(xs, ys):
        n = len(xs)
        if n < 2: return 0.0
        mx = sum(xs)/n; my = sum(ys)/n
        num = sum((xs[i]-mx)*(ys[i]-my) for i in range(n))
        den = sum((xs[i]-mx)**2 for i in range(n))
        return num/den if den else 0.0

    trends = {}
    for cat in ['Hail','Wind','Tornado','Flood','Lightning','Winter']:
        vals = [top_by_year_cat[y].get(cat, 0) for y in sorted_years]
        trends[cat] = {
            'slope_per_year': round(slope(sorted_years, vals), 2),
            'first_year': sorted_years[0] if sorted_years else None,
            'last_year': sorted_years[-1] if sorted_years else None,
            'first_year_count': vals[0] if vals else 0,
            'last_year_count': vals[-1] if vals else 0,
        }

    insights = {
        'generated_at': dt.datetime.utcnow().isoformat() + 'Z',
        'data_source': 'NOAA NCEI Storm Events Database',
        'years_covered': [min(available_years), max(available_years)],
        'years_count': len(available_years),
        'total_events': total_rows,
        'category_totals': dict(grand_totals.most_common()),
        'by_year': [
            {'year': y, 'total': top_by_year[y],
             'by_cat': {c: top_by_year_cat[y].get(c, 0) for c in
                        ['Hail','Wind','Tornado','Flood','Lightning','Winter','Other']}}
            for y in sorted_years
        ],
        'by_month': [{'month': m, 'count': top_by_month.get(m, 0)} for m in range(1, 13)],
        'state_rankings': {
            'recent_10y': [{'state': s, 'count': c} for s, c in top_by_state_recent.most_common(15)],
            'all_years': [{'state': s, 'count': c} for s, c in top_by_state_all.most_common(15)],
        },
        'top_hail_events': [
            {'magnitude_in': r[0], 'date': r[1], 'state': r[2], 'county': r[3], 'lat': r[4], 'lon': r[5]}
            for r in top_hail
        ],
        'top_tornadoes': [
            {'rank': r[0], 'tor_scale': r[1], 'date': r[2], 'state': r[3], 'county': r[4], 'lat': r[5], 'lon': r[6]}
            for r in top_tornadoes
        ],
        'top_wind_events': [
            {'speed_mph': r[0], 'date': r[1], 'state': r[2], 'county': r[3], 'lat': r[4], 'lon': r[5]}
            for r in top_wind
        ],
        'trends': trends,
    }
    with (out / 'insights.json').open('w') as f:
        json.dump(insights, f, separators=(',', ':'))
    print(f'Wrote {out / "insights.json"}')

    # Per-state files
    for state in STATES:
        sd = state_data[state]
        if not sum(sd['totals'].values()):
            continue
        sd['top_hail'].sort(key=lambda r: r[0], reverse=True); sd['top_hail'] = sd['top_hail'][:15]
        sd['top_tornadoes'].sort(key=lambda r: r[0], reverse=True); sd['top_tornadoes'] = sd['top_tornadoes'][:15]
        sd['top_wind'].sort(key=lambda r: r[0], reverse=True); sd['top_wind'] = sd['top_wind'][:15]
        state_yrs = sorted(sd['by_year'].keys())
        out_state = {
            'state': state,
            'generated_at': dt.datetime.utcnow().isoformat() + 'Z',
            'years_covered': [min(state_yrs), max(state_yrs)] if state_yrs else None,
            'totals': dict(sd['totals'].most_common()),
            'by_year': [
                {'year': y, 'total': sd['by_year'][y],
                 'by_cat': {c: sd['by_year_cat'][y].get(c, 0) for c in
                            ['Hail','Wind','Tornado','Flood','Lightning','Winter','Other']}}
                for y in state_yrs
            ],
            'by_month': [{'month': m, 'count': sd['by_month'].get(m, 0)} for m in range(1, 13)],
            'top_counties': [{'county': c, 'count': n} for c, n in sd['top_counties'].most_common(15)],
            'top_hail_events': [
                {'magnitude_in': r[0], 'date': r[1], 'county': r[3], 'lat': r[4], 'lon': r[5]}
                for r in sd['top_hail']
            ],
            'top_tornadoes': [
                {'rank': r[0], 'tor_scale': r[1], 'date': r[2], 'county': r[4], 'lat': r[5], 'lon': r[6]}
                for r in sd['top_tornadoes']
            ],
            'top_wind_events': [
                {'speed_mph': r[0], 'date': r[1], 'county': r[3], 'lat': r[4], 'lon': r[5]}
                for r in sd['top_wind']
            ],
        }
        with (out / 'by-state' / f'{state}.json').open('w') as f:
            json.dump(out_state, f, separators=(',', ':'))

    print(f'Wrote {len(STATES)} per-state files (where data exists)')
    print(f'Total events processed: {total_rows}')


def main():
    p = argparse.ArgumentParser(description='Build insights JSON from per-state SED CSVs.')
    p.add_argument('--src', default='./sed-data', help='Path to per-state SED CSV folders.')
    p.add_argument('--out', default='./data', help='Output directory.')
    args = p.parse_args()
    build(args.src, args.out)


if __name__ == '__main__':
    main()

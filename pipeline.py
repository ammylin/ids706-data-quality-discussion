"""Supplied investigation infrastructure: inspect all reachable stages, gate publication."""
import argparse
import csv
import hashlib
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SCHEMA = '''
CREATE TABLE IF NOT EXISTS raw_sales (
 row_id INTEGER PRIMARY KEY, build_id TEXT NOT NULL, delivery_id TEXT NOT NULL,
 window_start TEXT NOT NULL, revision INTEGER NOT NULL, schema_id TEXT NOT NULL,
 event_id TEXT NOT NULL, customer_id TEXT NOT NULL, event_time TEXT NOT NULL,
 amount_cents INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY, status TEXT NOT NULL,
 build_id TEXT NOT NULL, report TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS candidate_sales (run_id INTEGER NOT NULL, event_id TEXT,
 customer_id TEXT,event_time TEXT,amount_cents INTEGER,customer_version_id TEXT,region TEXT);
CREATE TABLE IF NOT EXISTS publication (singleton INTEGER PRIMARY KEY CHECK(singleton=1),run_id INTEGER NOT NULL);
CREATE VIEW IF NOT EXISTS published_sales AS SELECT c.* FROM publication p
 JOIN candidate_sales c ON c.run_id=p.run_id;
'''


def load_csv(path):
    with Path(path).open(newline='') as stream:
        return list(csv.DictReader(stream))


def metrics(rows):
    return {'rows': len(rows), 'distinct_events': len({r['event_id'] for r in rows}),
            'revenue_cents': sum(r['amount_cents'] for r in rows)}


def check(name, passed, detail):
    return {'rule': name, 'passed': bool(passed), 'detail': str(detail)}


def load_controls(path):
    # Compile fresh source on every invocation; edited controls never use stale .pyc files.
    namespace = {'__name__': 'case_controls', '__file__': str(path)}
    exec(compile(Path(path).read_text(), str(path), 'exec'), namespace)
    return namespace


def run(db_path, data, controls_path, sql_path):
    data, controls_path, sql_path = map(Path, (data, controls_path, sql_path))
    data = data.resolve()
    controls = load_controls(controls_path)
    build = hashlib.sha256(controls_path.read_bytes()).hexdigest()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    report = {'case_version': 'delivery-investigation-v2', 'build_id': build,
              'stages': {}, 'checks': [], 'diagnostics': [], 'selected_deliveries': []}
    received, history, active, candidate = [], [], [], []
    expected, inventory, chosen = [], [], []
    try:
        with db:
            run_id = db.execute("INSERT INTO runs(status,build_id,report) VALUES ('held',?,'{}')", (build,)).lastrowid
            report['run_id'] = run_id
            # Discover independent evidence before attempting any transformations.
            try:
                expected = load_csv(data/'expected_windows.csv')
                inventory = load_csv(data/'inventory.csv')
                history = load_csv(data/'customer_history.csv')
                report['expected'] = {'rows': sum(int(w['expected_events']) for w in expected),
                                      'revenue_cents': sum(int(w['expected_revenue_cents']) for w in expected)}
                chosen = controls['choose_deliveries']([dict(r) for r in inventory])
                if not isinstance(chosen, list) or any(r not in inventory for r in chosen):
                    raise ValueError('choose_deliveries must return inventory entries without altering metadata')
                report['selected_deliveries'] = chosen
            except Exception as exc:
                report['diagnostics'].append('Input/selection: '+str(exc))
            # Per-file errors are recorded; reachable evidence remains inspectable.
            for delivery in chosen:
                try:
                    path = (data/delivery['file']).resolve()
                    if not path.is_relative_to(data):
                        raise ValueError('Input file must be inside the data folder')
                    if hashlib.sha256(path.read_bytes()).hexdigest() != delivery['sha256']:
                        raise ValueError('File checksum does not agree with inventory')
                    rows = load_csv(path)
                    normalized = []
                    for row in rows:
                        datetime.strptime(row['event_time'], '%Y-%m-%dT%H:%M:%SZ')
                        if not row['event_id'].strip() or not row['customer_id'].strip():
                            raise ValueError('Required sale identifiers are blank')
                        cents = controls['normalize_amount'](row['amount'], delivery['schema_id'])
                        if type(cents) is not int or cents <= 0:
                            raise ValueError('Normalization must return a positive integer number of cents')
                        normalized.append(dict(build_id=build, delivery_id=delivery['delivery_id'],
                            window_start=delivery['window_start'], revision=int(delivery['revision']),
                            schema_id=delivery['schema_id'], event_id=row['event_id'],
                            customer_id=row['customer_id'], event_time=row['event_time'], amount_cents=cents))
                    received.extend(normalized)
                except Exception as exc:
                    report['diagnostics'].append(delivery.get('attempt_id','delivery')+': '+str(exc))
            if chosen:
                report['stages']['received'] = metrics(received)
                try:
                    controls['store_rows'](db, received, build)
                    ids = sorted({r['delivery_id'] for r in chosen})
                    slots = ','.join('?' for _ in ids)
                    active = [dict(r) for r in db.execute(
                        f'SELECT * FROM raw_sales WHERE build_id=? AND delivery_id IN ({slots}) ORDER BY row_id', [build,*ids])]
                    report['stages']['raw'] = metrics(active)
                    db.execute('CREATE TEMP TABLE active_sales AS SELECT event_id,customer_id,event_time,amount_cents FROM raw_sales WHERE 0')
                    db.executemany('INSERT INTO active_sales VALUES (?,?,?,?)',
                        [(r['event_id'],r['customer_id'],r['event_time'],r['amount_cents']) for r in active])
                    db.execute('CREATE TEMP TABLE customer_history (customer_version_id TEXT,customer_id TEXT,region TEXT,valid_from TEXT,valid_to TEXT)')
                    db.executemany('INSERT INTO customer_history VALUES (?,?,?,?,?)',
                        [(r['customer_version_id'],r['customer_id'],r['region'],r['valid_from'],r['valid_to'] or None) for r in history])
                    sql = sql_path.read_text().strip().rstrip(';')
                    candidate = [dict(r) for r in db.execute('SELECT * FROM (\n'+sql+'\n)')]
                    columns = ('event_id','customer_id','event_time','amount_cents','customer_version_id','region')
                    if any(set(row) != set(columns) for row in candidate):
                        raise ValueError('Transformation must return the six documented output columns')
                    if any(type(row['amount_cents']) is not int or row['amount_cents'] <= 0 for row in candidate):
                        raise ValueError('Candidate amounts must be positive integer cents')
                    db.executemany('INSERT INTO candidate_sales VALUES (?,?,?,?,?,?,?)',
                        [(run_id,*(r[k] for k in columns)) for r in candidate])
                    report['stages']['candidate'] = metrics(candidate)
                except Exception as exc:
                    report['diagnostics'].append('Ingestion/transformation: '+str(exc))
            # Supplied critical gates assess the candidate; they do not repair it.
            checks = report['checks']
            checks.append(check('reachable_stages', not report['diagnostics'] and 'candidate' in report['stages'],
                                '; '.join(report['diagnostics']) or 'Inputs and transformation were processed'))
            window_results = []
            for window in expected:
                rows = [r for r in active if r['window_start'] == window['window_start']]
                selected = [r for r in chosen if r['window_start'] == window['window_start']]
                window_results.append({'window_start':window['window_start'],
                    'expected_events':int(window['expected_events']), 'observed_rows':len(rows),
                    'distinct_events':len({r['event_id'] for r in rows}),
                    'expected_revenue_cents':int(window['expected_revenue_cents']),
                    'observed_revenue_cents':sum(r['amount_cents'] for r in rows),
                    'required_revision':int(window['required_revision']),
                    'selected_revisions':sorted({int(r['revision']) for r in selected})})
            report['windows'] = window_results
            checks.append(check('window_contract', bool(expected) and all(
                w['observed_rows']==w['distinct_events']==w['expected_events']
                and w['observed_revenue_cents']==w['expected_revenue_cents']
                and w['selected_revisions']==[w['required_revision']] for w in window_results),
                'Per-window event counts, revenue and revision agree with independent source controls'))
            checks.append(check('candidate_controls', bool(expected) and len(candidate)==report.get('expected',{}).get('rows')
                and sum(r['amount_cents'] for r in candidate)==report.get('expected',{}).get('revenue_cents'),
                'Candidate reconciles to the required full reporting period'))
            checks.append(check('event_uniqueness', len(candidate)==len({r['event_id'] for r in candidate}), 'One candidate row per business event'))
            source_facts = {(r['event_id'],r['customer_id'],r['event_time'],r['amount_cents']) for r in active}
            candidate_facts = {(r['event_id'],r['customer_id'],r['event_time'],r['amount_cents']) for r in candidate}
            checks.append(check('sales_facts', source_facts==candidate_facts, 'No active input event lost, invented or changed during enrichment; this does not prove input delivery completeness'))
            versions = {r['customer_version_id']:r for r in history}
            by_customer = {}
            for version in history:
                by_customer.setdefault(version['customer_id'],[]).append(version)
            def eligible(version,time):
                return version['valid_from'] <= time and (not version['valid_to'] or time < version['valid_to'])
            checks.append(check('historical_enrichment', all(
                (v:=versions.get(row['customer_version_id'])) is not None
                and v['customer_id']==row['customer_id'] and v['region']==row['region'] and eligible(v,row['event_time'])
                for row in candidate) and all(sum(eligible(v,row['event_time']) for v in by_customer.get(row['customer_id'],[]))==1 for row in active),
                'Selected historical attributes are correct and every active sale has one eligible version'))
            context={'inventory':inventory,'selected':chosen,'expected_windows':expected,
                     'received':received,'raw':active,'candidate':candidate,'history':history}
            try:
                extra = controls['quality_checks'](context)
                if not isinstance(extra,list) or any(not isinstance(c,dict) or not isinstance(c.get('rule'),str)
                    or type(c.get('passed')) is not bool or not isinstance(c.get('detail'),str) for c in extra):
                    raise ValueError('quality_checks returns a list of rule/passed/detail dictionaries')
                checks.extend(extra)
            except Exception as exc:
                checks.append(check('student_checks_execute',False,str(exc)))
            status='published' if all(c['passed'] for c in checks) else 'held'
            if status=='published':
                db.execute('INSERT INTO publication VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET run_id=excluded.run_id',(run_id,))
            live=[dict(row) for row in db.execute('SELECT * FROM published_sales ORDER BY event_id')]
            report['stages']['published']=metrics(live)
            pointer=db.execute('SELECT run_id FROM publication').fetchone()
            report.update(status=status,published_run_id=pointer[0] if pointer else None)
            report['stored_raw_rows']=db.execute('SELECT COUNT(*) FROM raw_sales').fetchone()[0]
            report['snapshots']={'raw':[{k:v for k,v in row.items() if k not in ('row_id','build_id')} for row in active],
                'candidate':candidate,'published':[{k:v for k,v in row.items() if k!='run_id'} for row in live]}
            report['snapshots']['raw'].sort(key=lambda r:(r['delivery_id'],r['event_id'],r['amount_cents']))
            report['snapshots']['candidate'].sort(key=lambda r:(r['event_id'],str(r['customer_version_id']),str(r['region'])))
            report['run_time_utc']=datetime.now(timezone.utc).isoformat()
            # Preserve the exact metadata and code used for later audit/diagnosis.
            report['inputs']={'inventory':inventory,'expected_windows':expected,'history':history,
                              'controls_source':controls_path.read_text(),'transform_sql':sql_path.read_text()}
            db.execute('UPDATE runs SET status=?,report=? WHERE run_id=?',(status,json.dumps(report),run_id))
        return report
    finally:
        db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,default=ROOT/'work/investigation.sqlite')
    commands=parser.add_subparsers(dest='command',required=True)
    execute=commands.add_parser('run')
    execute.add_argument('--data',type=Path,default=ROOT/('case_data' if (ROOT/'case_data').is_dir() else 'data'))
    execute.add_argument('--controls',type=Path,default=ROOT/'controls.py')
    execute.add_argument('--sql',type=Path,default=ROOT/'transform.sql')
    commands.add_parser('inspect')
    args=parser.parse_args()
    if args.command=='run':
        report=run(args.db,args.data,args.controls,args.sql)
        # Complete snapshots and input evidence are saved in the database, not dumped to the terminal.
        print(json.dumps({k:v for k,v in report.items() if k not in ('snapshots','inputs')},indent=2))
        return 0 if report['status']=='published' else 2
    with sqlite3.connect(args.db.resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        print(json.dumps([dict(row) for row in db.execute('SELECT run_id,status,build_id FROM runs')],indent=2))
    return 0

if __name__=='__main__':
    raise SystemExit(main())

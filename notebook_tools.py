"""Supplied notebook display/execution tools. No diagnosis or repair logic."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import pandas as pd
from IPython.display import display


class Investigation:
    def __init__(self, project, data, work):
        self.project, self.data, self.work = map(Path, (project,data,work))
        self.db = self.work/'investigation.sqlite'
        self.evidence = self.work/'evidence'
        self.evidence.mkdir(parents=True,exist_ok=True)

    def records(self,database,sql,params=()):
        with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as connection:
            connection.row_factory=sqlite3.Row
            return [dict(row) for row in connection.execute(sql,params)]

    def query(self,sql,params=(),database=None):
        result=pd.DataFrame(self.records(self.db if database is None else database,sql,params))
        display(result.head(200))
        print(f'Showing {min(len(result),200):,} of {len(result):,} rows.')
        return result

    def saved_runs(self):
        return [json.loads(p.read_text()) for p in sorted(self.evidence.glob('run-*.json'))]

    def show_measurements(self,captures=None):
        rows=[]
        for saved in self.saved_runs() if captures is None else captures:
            report=saved['report']
            for stage in ('received','raw','candidate','published'):
                values=report['stages'].get(stage)
                cents=values['revenue_cents'] if values is not None else None
                rows.append({'Label':saved['label'],'Run':report['run_id'],
                             'Build':report['build_id'][:10],'Stage':stage,
                             'Rows':values['rows'] if values is not None else 'N/A',
                             'Distinct events':values['distinct_events'] if values is not None else 'N/A',
                             'Revenue cents':cents if cents is not None else 'N/A',
                             'Decision':report['status']})
        display(pd.DataFrame(rows))

    def show_run(self,saved):
        report=saved['report']
        print(f"{saved['label']}: run {report['run_id']}, {report['status']}")
        if report['diagnostics']:
            display(pd.DataFrame({'Diagnostic':report['diagnostics']}))
        display(pd.DataFrame(report['checks'])[['rule','passed']])
        self.show_measurements([saved])
        windows=pd.DataFrame(report['windows'])
        if not windows.empty:
            problems=windows.loc[(windows.observed_rows!=windows.expected_events)
                |(windows.distinct_events!=windows.expected_events)
                |(windows.observed_revenue_cents!=windows.expected_revenue_cents)
                | windows.apply(lambda r:r['selected_revisions']!=[r['required_revision']],axis=1)]
            print('Windows requiring review (full results remain in the saved report):')
            display(problems)

    def run_case(self,label,data=None,database=None):
        database=self.db if database is None else Path(database)
        command=[sys.executable,'-B',str(self.project/'pipeline.py'),'--db',str(database),
                 'run','--data',str(self.data if data is None else data)]
        process=subprocess.run(command,cwd=self.project,capture_output=True,text=True)
        if process.returncode not in (0,2):
            raise RuntimeError(process.stderr or process.stdout or 'Pipeline could not start.')
        summary=json.loads(process.stdout)
        report=json.loads(self.records(database,'SELECT report FROM runs WHERE run_id=?',(summary['run_id'],))[0]['report'])
        saved={'label':label,'database':str(database),'report':report}
        path=self.evidence/f'run-{len(self.saved_runs())+1:04d}.json'
        with path.open('x') as stream:json.dump(saved,stream,indent=2)
        print('Evidence saved:',path.name)
        self.show_run(saved)
        return saved

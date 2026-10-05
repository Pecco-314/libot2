"""Derive a separately versioned candidate with complete replay lineage."""
import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.build_historical_source_database import finalize_evidence
from scripts.audit_mit3uri_danmaku_integrity import save_json,save_csv


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if not (a.source/'summary.json').exists():raise ValueError('Completed source candidate required')
    if a.source.resolve()==a.output.resolve() or (a.output/'audit.json').exists():raise ValueError('Separate, unaudited output directory required')
    a.output.mkdir(exist_ok=True)
    target=a.output/'rebuilt.sqlite3'
    with closing(sqlite3.connect(a.source.joinpath('rebuilt.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)) as source, closing(sqlite3.connect(target)) as out, closing(sqlite3.connect(store.ARCHIVE_DB.as_uri()+'?mode=ro',uri=True)) as cold:
        if not out.execute("SELECT 1 FROM sqlite_master WHERE name='event'").fetchone():source.backup(out,pages=4096)
        finalize_evidence(out,cold)
        report=json.loads((a.source/'summary.json').read_text())
        report['counts']=dict(out.execute('SELECT cmd,COUNT(*) FROM event GROUP BY cmd'))
        report['source_counts']=dict(out.execute('SELECT source,COUNT(*) FROM origin GROUP BY source'))
        report['review_counts']=dict(out.execute('SELECT kind,COUNT(*) FROM review GROUP BY kind'))
        report['parent_candidate']=str(a.source.resolve())
        if out.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Candidate integrity check failed')
        with out:out.execute("INSERT OR REPLACE INTO meta VALUES('build_summary',?)",(json.dumps(report),))
        save_json(a.output/'summary.json',report)
        save_csv(a.output/'sessions.csv',[json.loads(r[0]) for r in out.execute('SELECT report FROM build_session')])
        print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)


if __name__=='__main__':main()

"""Measure cold-table removal on a disposable copy, never on the live database."""
import json
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

from src.db import danmaku_sources as store
from scripts.migrate_danmaku_cold_storage import REPORT


def main():
    if not json.loads(REPORT.read_text()).get('copy_verified'):
        raise RuntimeError('Complete verified cold copy required')
    results={}
    with tempfile.TemporaryDirectory(prefix='cold-detach-check-',dir=store.ARCHIVE_DB.parent) as d:
        path=Path(d)/'disposable.sqlite3'
        shutil.copyfile(store.ARCHIVE_DB,path)
        with sqlite3.connect(path) as c:
            c.execute('PRAGMA journal_mode=WAL')
            c.execute('PRAGMA secure_delete=OFF')
            for table in ('danmaku_source_link','danmaku_merge_review','danmaku_removed_event','danmaku_source_checkpoint','danmaku_source_record','danmaku_source_artifact'):
                start=time.monotonic()
                c.execute(f'DROP TABLE {table}');c.commit()
                results[table]=round(time.monotonic()-start,3)
                print(table,results[table],flush=True)
    (store.ROOT/'data/reports/cold_detach_benchmark.json').write_text(json.dumps(results,indent=2)+'\n')


if __name__=='__main__':main()

"""Retain cached SC responses and import receipts in the cold evidence store."""
import json
import sqlite3
from contextlib import closing

from src.db import danmaku_sources as store


def main():
    root=store.ROOT/'data/imports/qianqiu_sc_20260924'
    paths=[root/(month+'.json') for month in ('2025-11','2025-12','2026-01','2026-02','2026-03','2026-04')]
    paths.append(root/'run_20260924_220632_074465/applied.json')
    result=[]
    with closing(sqlite3.connect(store.ARCHIVE_DB,timeout=1)) as c:
        for path in paths:
            with c:
                aid=store.artifact(c,'qqzy',str(path.relative_to(root)),path.read_bytes(),dict(event_kind='sc',source_file=str(path),cutoff_ms=store.EXTERNAL_CUTOFF_MS,evidence_only=True))
            result.append(dict(path=str(path),artifact_id=aid))
    print(json.dumps(dict(production_modified=False,artifacts=result),ensure_ascii=False,indent=2))


if __name__=='__main__':main()

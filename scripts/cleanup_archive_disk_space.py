"""Remove approved, explicitly scoped caches and obsolete copies; retain a manifest."""
import argparse
import json
import os
import shutil
import tarfile
import time
from pathlib import Path

ROOT=Path('/home/pecco/libot2')
DIAG=ROOT/'data/diagnostics/napcat-native-debug-20260924'
SERVERS=Path('/home/pecco/.vscode-server/cli/servers')


def active_references(target):
    found=[]
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit() or proc.stat().st_uid!=os.getuid():
            continue
        try:
            text=(proc/'cmdline').read_bytes().decode(errors='replace')
            for link in ('exe','cwd'):
                try:text+='\n'+os.readlink(proc/link)
                except OSError:pass
            if str(target) in text:found.append(int(proc.name))
        except (PermissionError,FileNotFoundError,ProcessLookupError):pass
    return found


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    dirs=[Path('/home/pecco/.cache/pip'),Path('/home/pecco/.npm/_cacache'),
          DIAG/'official-current',DIAG/'compat-3.2.25',
          ROOT/'data/backups/qq-profile-before-20260919-upgrade']
    versions=sorted(SERVERS.glob('Stable-*'),key=lambda p:p.stat().st_mtime,reverse=True)
    dirs.extend(versions[1:])
    files=[ROOT/'data/backups/libot.bk.db']
    deadline=time.time()-7*86400
    for name in ('events','dc','song_list','fan_club','stats'):
        for path in (ROOT/'data/images'/name).rglob('*'):
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime<deadline:
                files.append(path)
    if not (ROOT/'data/backups/libot_20261003_050000.db').is_file():
        raise RuntimeError('Expected retained database backup is absent')
    report={'apply':args.apply,'directories':[],'files':[],'skipped_active':{},
            'preserved_vscode_version':str(versions[0]) if versions else None}
    for target in dirs:
        if not target.exists():continue
        if target.is_symlink() or target.resolve()!=target:
            raise RuntimeError(f'Unexpected symlink target: {target}')
        active=active_references(target)
        if active:
            report['skipped_active'][str(target)]=active
            continue
        report['directories'].append(str(target))
    report['files']=[str(p) for p in files if p.is_file()]
    manifest=ROOT/'data/reports/disk_cleanup_20261004.json'
    manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    if args.apply:
        # Keep small case-level metadata alongside the diagnostic scripts/logs.
        metadata_archive=DIAG/'retained_case_metadata_20261004.tar.gz'
        with tarfile.open(metadata_archive if not metadata_archive.exists() else DIAG/f'retained_case_metadata_{time.time_ns()}.tar.gz','w:gz') as archive:
            for directory in (DIAG/'official-current',DIAG/'compat-3.2.25'):
                if directory.exists():
                    for path in directory.iterdir():
                        if path.is_file() and not path.is_symlink() and path.stat().st_size<20*1024*1024:
                            archive.add(path,arcname=str(path.relative_to(DIAG)))
        for name in report['files']:
            Path(name).unlink(missing_ok=True)
        for name in report['directories']:
            shutil.rmtree(name)
        report['finished']=True
        manifest.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='files'},ensure_ascii=False,indent=2))
    print('individual_files:',len(report['files']))


if __name__=='__main__':main()

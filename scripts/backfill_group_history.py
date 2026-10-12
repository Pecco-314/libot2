"""经本机 NapCat WebUI 认证补群消息缓存，不向群发送消息。"""

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from src.common.group_history import recover_group_history
from src.common.utils import load_env_file
from src.db import recall


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('group_id',type=int)
    parser.add_argument('--since',required=True,help='北京时间 YYYY-MM-DD HH:MM:SS')
    parser.add_argument('--max-pages',type=int,default=40)
    args = parser.parse_args()
    since = int(datetime.fromisoformat(args.since).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp())
    load_env_file()
    config_path = Path(os.environ['NAPCAT_PATH'])/'opt/QQ/resources/app/app_launcher/napcat/config/webui.json'
    config = json.loads(config_path.read_text())
    base = f"http://127.0.0.1:{int(config.get('port') or 6099)}"
    recall.init_db()
    async with httpx.AsyncClient(trust_env=False,timeout=45) as client:
        login = (await client.post(base+'/api/auth/login',json={
            'hash':hashlib.sha256((config['token']+'.napcat').encode()).hexdigest(),
        })).json()
        if login.get('code') != 0:
            raise RuntimeError(f"NapCat WebUI authentication failed: {login.get('message')}")
        client.headers['Authorization'] = 'Bearer '+login['data']['Credential']

        async def call_api(action, **params):
            response = await client.post(base+'/api/Debug/call',json={'action':action,'params':params})
            response.raise_for_status()
            wrapper = response.json()
            body = wrapper.get('data') or {}
            if wrapper.get('code') != 0 or body.get('retcode') != 0:
                raise RuntimeError(f"NapCat API failed: {wrapper.get('message')} / {body.get('message')}")
            return body['data']

        result = await recover_group_history(call_api,args.group_id,since,max_pages=args.max_pages)
        if result['oldest']:
            result['oldest'] = datetime.fromtimestamp(result['oldest'],ZoneInfo('Asia/Shanghai')).isoformat()
        print(json.dumps(result,ensure_ascii=False))
        if not result['complete']:
            raise SystemExit('历史尚未追到起点，可再次执行并适当增加 --max-pages；已补消息不会重复入库。')


if __name__ == '__main__':
    asyncio.run(main())

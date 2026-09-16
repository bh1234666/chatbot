"""Select legacy HTTP or current WebSocket ingress without changing QQ identity.

Stop the other project's services before switching; never kill active tasks.
Credentials stay local and are never printed.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import socket
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def listening(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False


def configure(config, mode):
    result = copy.deepcopy(config)
    clients = result.setdefault('network', {}).setdefault('httpClients', [])
    matches = [c for c in clients if c.get('name') == 'chatbot-bridge']
    if len(matches) > 1:
        raise RuntimeError('Multiple chatbot-bridge entries; refusing ambiguous configuration')
    if not matches:
        clients.append({'name': 'chatbot-bridge', 'messagePostFormat': 'array', 'token': ''})
        matches = clients[-1:]
    matches[0].update(enable=mode == 'legacy', url='http://127.0.0.1:8090/napcat/callback')
    return result


def switch(mode):
    if mode == 'legacy':
        state = ROOT / 'deployments/r9-20260910/user-data/supervisor-state.json'
        port = json.loads(state.read_text('utf-8')).get('port', 57769) if state.exists() else 57769
        if listening(int(port)):
            raise RuntimeError('Current project is running. Close its control window before starting legacy.')
    elif any(listening(p) for p in (8000, 8090)):
        raise RuntimeError('Legacy services are running. Close legacy API and Bridge windows before starting current.')

    paths = list((ROOT / 'napcat').glob('NapCat.*.Shell/versions/*/resources/app/napcat/config/webui.json'))
    if len(paths) != 1:
        raise RuntimeError('Cannot uniquely identify NapCat installation')
    web = json.loads(paths[0].read_text('utf-8-sig'))
    port = int(web.get('port', 6099))
    if listening(port):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        credential = ''

        def call(route, data):
            headers = {'Content-Type': 'application/json'}
            if credential:
                headers['Authorization'] = 'Bearer ' + credential
            request = urllib.request.Request(f'http://127.0.0.1:{port}/api/{route}',
                                             json.dumps(data).encode(), headers)
            with opener.open(request, timeout=15) as response:
                payload = json.load(response)
            if payload.get('code') != 0:
                raise RuntimeError('NapCat rejected ' + route + '; configuration not confirmed')
            return payload.get('data')

        digest = hashlib.sha256((web['token'] + '.napcat').encode()).hexdigest()
        credential = call('auth/login', {'hash': digest})['Credential']
        previous = call('OB11Config/GetConfig', {})
        desired = configure(previous, mode)
        if desired != previous:
            backup = paths[0].parent / 'onebot.before-project-switch.private.json'
            if not backup.exists():
                backup.write_text(json.dumps(previous, ensure_ascii=False, indent=2), 'utf-8')
            call('OB11Config/SetConfig', {'config': json.dumps(desired)})
        if call('OB11Config/GetConfig', {}) != desired:
            raise RuntimeError('NapCat configuration readback mismatch')
    else:
        configs = [p for p in paths[0].parent.glob('onebot11_*.json')
                   if p.stem.removeprefix('onebot11_').isascii()
                   and p.stem.removeprefix('onebot11_').isdigit()]
        if len(configs) != 1:
            raise RuntimeError('Cannot uniquely identify offline QQ account')
        path = configs[0]
        original = path.read_text('utf-8-sig')
        desired = configure(json.loads(original), mode)
        backup = path.with_suffix('.before-project-switch.private.json')
        if not backup.exists():
            backup.write_text(original, 'utf-8')
        temporary = path.with_suffix('.switch.tmp')
        temporary.write_text(json.dumps(desired, ensure_ascii=False, indent=2), 'utf-8')
        temporary.replace(path)
    print('QQ ingress configured: ' + mode + (' (HTTP 8090)' if mode == 'legacy' else ' (WebSocket; legacy HTTP disabled)'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('legacy', 'current'))
    args = parser.parse_args()
    try:
        switch(args.mode)
    except Exception as exc:
        # Do not expose response bodies or authentication material.
        print('QQ mode switch failed: ' + (str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__))
        raise SystemExit(1)

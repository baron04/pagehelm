"""Guarded application-API helpers for an explicitly requested synthetic refresh."""
from __future__ import annotations

import json
import os
from pathlib import Path

from seed import ApiError, Client


class DemoClient(Client):
    def __init__(self, base, tenant, journal: Path):
        super().__init__(base, tenant)
        self.journal = journal
        if journal.exists():
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            pending = {}
            for event in events:
                if event['state'] == 'dispatch':
                    pending[event['sequence']] = event
                else:
                    pending.pop(event['sequence'], None)
                if event['state'] == 'unknown':
                    raise RuntimeError('Uncertain write in journal; inspect application before any new run')
            if pending:
                raise RuntimeError('Unacknowledged dispatch; inspect application before any new run')
        self.sequence = sum(1 for _ in journal.open()) if journal.exists() else 0
        self.uncertain = False

    def event(self, **event):
        with self.journal.open('a') as out:
            out.write(json.dumps(event, ensure_ascii=False) + '\n')
            out.flush()
            os.fsync(out.fileno())

    def call(self, method, path, body=None, query=None):
        if method == 'GET':
            return super().call(method, path, body, query)
        if self.uncertain:
            raise RuntimeError('Uncertain write blocks further dispatch')
        self.sequence += 1
        seq = self.sequence
        # Never journal request bodies, tokens or account credentials.
        self.event(sequence=seq, state='dispatch', method=method, path=path)
        try:
            result = super().call(method, path, body, query)
        except ApiError as exc:
            self.uncertain = isinstance(exc.code, int) and 500 <= exc.code < 600
            self.event(sequence=seq, state='unknown' if self.uncertain else 'rejected', code=exc.code, path=path)
            raise
        except Exception:
            self.uncertain = True
            self.event(sequence=seq, state='unknown', path=path)
            raise
        self.event(sequence=seq, state='acknowledged', path=path)
        return result


class Records:
    def __init__(self, client):
        self.c, self.cache, self.created = client, {}, {}

    def rows(self, route, **params):
        key = (route, tuple(sorted(params.items())))
        if key not in self.cache:
            result, page = [], 1
            while True:
                data = self.c.get(route, pageNo=page, pageSize=100, **params)
                if isinstance(data, list):
                    result = data
                    break
                batch = data['list']
                result.extend(batch)
                if len(result) >= data['total']:
                    break
                if not batch or page >= 1000:
                    raise RuntimeError('Incomplete pagination: ' + route)
                page += 1
            ids = [r['id'] for r in result if 'id' in r]
            if len(set(ids)) != len(ids):
                raise RuntimeError('Duplicate paginated records: ' + route)
            self.cache[key] = result
        return self.cache[key]

    def ensure(self, route, body, *, key='name', listing='page', params=None):
        params = params or {}
        values = self.rows(route + '/' + listing, **params)
        def normalized(value):
            # This application serializes LocalDate as midnight epoch milliseconds.
            if key == 'date' and isinstance(value, (int, float)):
                return int(value + 8 * 3600000) // 86400000
            return value
        matches = [row for row in values if normalized(row.get(key)) == normalized(body[key])]
        if len(matches) > 1:
            raise RuntimeError('Non-unique synthetic key: ' + route)
        if matches:
            return matches[0]['id']
        ident = self.c.post(route + '/create', body)
        if not isinstance(ident, (int, str)) or isinstance(ident, bool) or not ident:
            raise RuntimeError('Create acknowledged without record id: ' + route)
        values.append({**body, 'id': ident})
        self.created[route] = self.created.get(route, 0) + 1
        return ident


REMOVE = {'作者动态': 'https://www.iocoder.cn', 'Boot 开发文档': 'https://doc.iocoder.cn',
          'Cloud 开发文档': 'https://cloud.iocoder.cn'}
DISABLE_PATHS = {'/iot', '/report', '/cms', '/eam'}
HIDE_PATHS = {'/ai', '/mp', '/im', '/pay'}
HIDE_COMPONENTS = {
    'infra/codegen/index', 'infra/testDemo/index', 'infra/build/index',
    'infra/dataSourceConfig/index', 'infra/druid/index', 'infra/redis/index',
    'infra/server/index', 'infra/job/index',
}


def menu_scope(menus):
    """Return exactly scoped mutations plus ordinary exploration-role grants."""
    by_id = {m['id']: m for m in menus}
    remove = {m['id'] for m in menus if m['name'] in REMOVE
              and (m.get('path') or '').rstrip('/') == REMOVE[m['name']] and m['parentId'] == 0}
    disabled = {m['id'] for m in menus if m['parentId'] == 0 and m['path'] in DISABLE_PATHS}
    hidden = {m['id'] for m in menus if (m['parentId'] == 0 and m['path'] in HIDE_PATHS)
              or (m.get('component') or '').removesuffix('.vue') in HIDE_COMPONENTS}
    for m in menus:
        if (m.get('component') or '').startswith(('infra/demo/', 'pay/demo/')):
            hidden.add(m['id'])
    def below(ident, roots):
        seen = set()
        while ident in by_id and ident not in seen:
            if ident in roots:
                return True
            seen.add(ident)
            ident = by_id[ident]['parentId']
        return False
    disabled = {i for i in by_id if below(i, disabled)}
    hidden = {i for i in by_id if below(i, hidden)}
    excluded = remove | disabled | hidden
    # Grant menus and read buttons only. No role/user administration or write buttons.
    grants = []
    for m in menus:
        if m['id'] in excluded or m.get('status', 0) != 0:
            continue
        perm = m.get('permission', '') or ''
        if (not perm and m['type'] != 3) or perm.endswith((':query', ':list')):
            grants.append(m['id'])
    return {'remove': sorted(remove), 'disable': sorted(disabled),
            'hidden_from_explorer': sorted(hidden), 'explorer_grants': sorted(grants)}

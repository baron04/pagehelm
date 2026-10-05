"""Opt-in standard Odoo read effects, bound to a deployment handler review.

Catalog membership does not certify effects. The caller must supply a review of the
standard framework handlers and the exact installed-module digest. No button-name list
or old allow-click verdict becomes authority here. Business forms remain in FormPort.
"""
from __future__ import annotations

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urljoin, urlsplit

from web_operation_agent.business.errors import BusinessToolIdentityError
from web_operation_agent.execution.action_admission import (
    ActionAdmissionRefused, ActionEffect, ActionPermit, ActionResultPending,
)
from .server.descriptors import OdooRuntimeDescriptorReader
from .server.generic_reads import odoo_model_allowed


class OdooReadActions:
    application_id = 'odoo'
    required_scope = 'generic_records:read'
    actions = frozenset({'goto', 'click'})

    def __init__(self, client, *, review: str, module_digest: str):
        if not isinstance(review, str) or not review.strip() or len(review) > 200:
            raise ValueError('standard Odoo effects require a deployment handler review')
        if not isinstance(module_digest, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', module_digest):
            raise ValueError('the effect review must bind the installed module digest')
        self.client, self.review, self.module_digest = client, review, module_digest
        self.origin = client.base_url.rstrip('/')
        self.reader = OdooRuntimeDescriptorReader(client)
        self._turn, self._checks = None, {}

    def _catalog(self, context, checkpoint):
        context.require_live(application_id=self.application_id, required_scope=self.required_scope)
        checkpoint()
        actor = self.client.resolve_delegated_identity()
        checkpoint()
        if any(getattr(actor, key) != getattr(context.principal, key)
               for key in ('tenant_id', 'workspace_id', 'actor_id')):
            raise BusinessToolIdentityError('the Odoo delegated actor changed')
        catalog = self.reader.catalog()
        checkpoint()
        if catalog.module_digest != self.module_digest:
            raise ActionAdmissionRefused('unreviewed')
        turn = context.principal.binding_digest, context.session_id, context.turn_id
        if turn != self._turn:
            self._checks.clear()
            self._turn = turn
        return catalog

    def _path(self, url):
        parsed = urlsplit(url)
        if (f'{parsed.scheme}://{parsed.netloc}' != self.origin or parsed.fragment or
                not re.fullmatch(r'/(?!/)[a-zA-Z0-9/_-]*', parsed.path) or
                any(p in {'.', '..'} for p in parsed.path.split('/'))):
            raise ActionAdmissionRefused('invalid_target')
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise ActionAdmissionRefused('invalid_target') from None
        if query and (len(query) != 1 or query[0][0] != 'view_type'
                      or query[0][1] not in {'list', 'kanban', 'form'}):
            raise ActionAdmissionRefused('invalid_target')
        return parsed.path, query[0][1] if query else ''

    @staticmethod
    def _action(catalog, path):
        models = {m.model: m for m in catalog.models}
        candidates = []
        for a in catalog.actions:
            base = '/odoo/' + a.route
            model = models.get(a.model)
            if (a.readable and model and not model.transient and odoo_model_allowed(a.model)
                    and not re.search(r'\bactive_(?:id|ids|model)\b', a.domain + ' ' + a.context)
                    and (path == base or re.fullmatch(re.escape(base) + r'/[1-9][0-9]*', path))):
                candidates.append(a)
        if len(candidates) != 1:
            raise ActionAdmissionRefused('unreviewed')
        return candidates[0]

    def admit(self, intent, *, context, checkpoint):
        catalog = self._catalog(context, checkpoint)
        before = intent.observation
        path, _ = self._path(before.url)
        if before.page_route != path:
            raise ActionAdmissionRefused('invalid_target')
        current = self._action(catalog, path)
        effect, expected = ActionEffect.NAVIGATION, None
        if intent.action == 'goto':
            target, view = self._path(urljoin(before.url, intent.arguments['url']))
            destination = self._action(catalog, target)
            if view and view not in destination.view_modes:
                raise ActionAdmissionRefused('unreviewed')
            expected = ['page', target, view]
        else:
            element = next(e for e in before.elements if e.ref == intent.arguments['ref'])
            attrs = element.attrs
            if attrs.get('disabled') == 'true':
                raise ActionAdmissionRefused('invalid_target')
            if attrs.get('page_control') == 'pagination' and before.page_read:
                page, size = int(attrs['page_number']), int(attrs['page_size'])
                if (before.active_view not in {'List', 'Kanban'} or page < 1
                        or size != before.page_read['page_size']
                        or abs(page - before.page_read['page']) != 1):
                    raise ActionAdmissionRefused('invalid_target')
                effect = ActionEffect.READ_ONLY
                expected = ['pager', path, page, size, before.page_read['query_digest'],
                            before.page_read['revision']]
            elif element.role == 'tab' and before.record_route:
                checkpoint()
                view = self.client.call(current.model, 'get_view', [], {'view_type': 'form'})
                checkpoint()
                arch = view.get('arch') if isinstance(view, dict) else None
                if not isinstance(arch, str) or len(arch) > 512000 or re.search(r'<!\s*(?:DOCTYPE|ENTITY)', arch, re.I):
                    raise ActionAdmissionRefused('unreviewed')
                try:
                    root = ET.fromstring(arch)
                except ET.ParseError:
                    raise ActionAdmissionRefused('unreviewed') from None
                names = [p.get('string') for p in root.iter('page')]
                if names.count(element.name) != 1 or sum(e.role == 'tab' and e.name == element.name for e in before.elements) != 1:
                    raise ActionAdmissionRefused('unreviewed')
                effect = ActionEffect.READ_ONLY
                expected = ['tab', path, before.record_route, element.name]
            else:
                href = attrs.get('href')
                if element.role == 'link' and href and attrs.get('target', '') in {'', '_self'}:
                    target, view = self._path(urljoin(before.url, href))
                    self._action(catalog, target)
                    expected = ['page', target, view]
        if expected is None:
            raise ActionAdmissionRefused('unreviewed')
        condition = hashlib.sha256(json.dumps(expected, sort_keys=True).encode()).hexdigest()
        self._checks[intent.binding_digest] = condition, catalog.descriptor_digest, expected
        return ActionPermit(intent.binding_digest, effect, self.review, condition)

    def verify(self, permit, intent, receipt, *, observation, context, checkpoint):
        catalog = self._catalog(context, checkpoint)
        saved = self._checks.get(intent.binding_digest)
        if not saved or saved[:2] != (permit.postcondition, catalog.descriptor_digest):
            return False
        _, _, expected = saved
        path, _ = self._path(observation.url)
        if observation.page_route != path:
            raise ActionResultPending()
        self._action(catalog, path)
        if expected[0] == 'page':
            if path != expected[1]:
                raise ActionResultPending()
            return not expected[2] or (observation.active_view or '').casefold() == expected[2]
        if path != expected[1] or observation.url != intent.observation.url:
            return False
        if expected[0] == 'pager':
            read = observation.page_read
            if not read or read['revision'] <= expected[5]:
                raise ActionResultPending()
            return (read['page'], read['page_size'], read['query_digest']) == tuple(expected[2:5])
        if observation.record_route != expected[2]:
            return False
        matches = [e for e in observation.elements if e.role == 'tab' and e.name == expected[3]]
        return len(matches) == 1 and matches[0].attrs.get('selected') == 'true'

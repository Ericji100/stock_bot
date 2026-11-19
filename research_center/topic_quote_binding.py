"""Bind selected evidence spans to immutable source text, not model-written quotes."""
from copy import deepcopy
import hashlib
import re

from .source_date_normalizer import normalize_published_date
from .source_identity import reference_sources, source_url
from .source_text_cleaner import source_body_text


def quote_catalog(sources):
    result = []
    for source in sources:
        body = source_body_text(source)
        digest = hashlib.sha256(body.encode()).hexdigest()
        start = 0
        while start < len(body):
            end = min(start + 1200, len(body))
            if end < len(body):
                boundary = body.rfind('\n', start + 16, end)
                if boundary > start:
                    end = boundary + 1
            if len(body) - end < 16:
                end = len(body)
            identity = f"{source['source_id']}:{digest}:{start}:{end}"
            result.append({'quote_id': 'Q' + hashlib.sha256(identity.encode()).hexdigest()[:24],
                'source_id': source['source_id'], 'url': source.get('url'),
                'publish_date': normalize_published_date(source.get('published_date')),
                'body_sha256': digest, 'start': start, 'end': end, 'text': body[start:end]})
            start = end
    return result


def _rows(value, path=''):
    if isinstance(value, dict):
        yield path, value
        for key, child in list(value.items()):
            yield from _rows(child, f'{path}/{key}')
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _rows(child, f'{path}/{index}')


def bind_topic_quotes(pack, data):
    from .topic_input_package import merge_topic_sources
    sources = merge_topic_sources(data)
    catalog = {row['quote_id']: row for row in quote_catalog(sources)}
    bindings, failures = [], []
    targets = [(f'/actions/{index}', action.to_dict(), index) for index, action in enumerate(pack.actions)]
    if pack.company_knowledge_updates:
        targets.append(('/company_knowledge_updates', deepcopy(pack.company_knowledge_updates), None))
    for prefix, payload, index in targets:
        for path, row in _rows(payload, prefix):
            if row.get('quote_id'):
                span = catalog.get(row['quote_id'])
                day = normalize_published_date(row.get('publish_date') or row.get('published_date'))
                matches = (span and (not row.get('url') or source_url(row['url']) == source_url(span['url']))
                    and (not day or day == span['publish_date'])
                    and (not row.get('source_id') or any(candidate['source_id'] == span['source_id']
                         for candidate in reference_sources(row['source_id'], sources,
                         url=span['url'], published_date=span['publish_date']))))
                if not matches:
                    failures.append({'path': path, 'reason': 'quote_reference_mismatch', 'quote_id': row['quote_id']})
                    continue
                original = deepcopy(row)
                row.update(source_id=span['source_id'], url=span['url'], publish_date=span['publish_date'],
                           supporting_quote=span['text'])
                bindings.append({'path': path, 'policy': 'immutable_source_span', 'original': original,
                                 'quote_id': span['quote_id'], 'body_sha256': span['body_sha256']})
            elif row.get('source_id') and (row.get('url') or row.get('source_url')):
                matches = reference_sources(row['source_id'], sources, url=row.get('url') or row['source_url'],
                    published_date=row.get('publish_date') or row.get('published_date'))
                if len(matches) == 1 and row['source_id'] != matches[0]['source_id']:
                    bindings.append({'path': path, 'policy': 'exact_url_date_reference', 'original': deepcopy(row)})
                    row['source_id'] = matches[0]['source_id']
        for path, row in _rows(payload, prefix):
            refs = row.get('evidence_source_ids')
            if not isinstance(refs, list):
                continue
            code = str(row.get('company_code') or row.get('code') or '')
            evidence = list(row.get('evidence') or [])
            if code:
                evidence.extend(item for _, claim in _rows(payload)
                    if str(claim.get('company_code') or claim.get('code') or '') == code
                    for item in claim.get('evidence') or [] if isinstance(item, dict))
            for offset, ref in enumerate(refs):
                candidates = reference_sources(ref, sources)
                if len(candidates) == 1:
                    selected = candidates
                else:
                    selected = [source for source in candidates if any(
                        source_url(item.get('url')) == source_url(source.get('url'))
                        and len(''.join(str(item.get('supporting_quote') or '').split())) >= 16
                        and ''.join(item['supporting_quote'].split()) in ''.join(source_body_text(source).split())
                        and (not item.get('publish_date') or normalize_published_date(item['publish_date']) == normalize_published_date(source.get('published_date')))
                        for item in evidence if isinstance(item, dict))]
                if len(selected) == 1 and ref != selected[0]['source_id']:
                    bindings.append({'path':f'{path}/evidence_source_ids/{offset}',
                        'policy':'same_company_exact_evidence_reference', 'original':ref,
                        'source_id':selected[0]['source_id']})
                    refs[offset] = selected[0]['source_id']
        if index is None:
            pack.company_knowledge_updates = payload
        else:
            pack.actions[index] = type(pack.actions[index]).from_dict(payload)
    pack.extra['quote_binding_audit'] = {'bindings': bindings, 'issues': failures}
    return pack.extra['quote_binding_audit']


def extend_short_source_quote(quote, body):
    """Extend only one exact short occurrence; never repair paraphrases or numbers."""
    if not quote or not 8 <= len(''.join(quote.split())) < 16:
        return None
    offset = body.find(quote)
    if offset < 0 or body.find(quote, offset + 1) >= 0:
        return None
    start = max(body.rfind('\n', 0, offset), body.rfind('。', 0, offset)) + 1
    end = offset + len(quote)
    tail = re.search(r'[。\n]', body[end:])
    end = end + tail.end() if tail else len(body)
    excerpt = body[start:end]
    return excerpt if len(''.join(excerpt.split())) >= 16 else None


def topic_repair_plan(pack, audit):
    actions = []
    for index, action in enumerate(pack.actions):
        prefix = f'/actions/{index}'
        issues = [row for row in audit['issues'] if row['path'] == prefix or row['path'].startswith(prefix + '/')]
        actions.append({'theme_id': action.theme_id, 'status': 'blocked' if issues else 'citation_checks_passed',
                        'issues': issues})
    return {'actions': actions, 'blocked_theme_ids': [row['theme_id'] for row in actions if row['issues']],
            'other_issues': [row for row in audit['issues'] if not row['path'].startswith('/actions/')],
            'automatic_paid_repair': False, 'whole_pack_applicable': audit['passed'],
            'scope': '引用檢核分題結果，不代表公司關係已證實；不允許部分自動套用。'}

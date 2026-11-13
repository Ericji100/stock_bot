"""Stable, lossless document IDs and strict legacy-reference resolution."""
from copy import deepcopy
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit

from .source_date_normalizer import normalize_published_date


def source_url(value):
    try:
        parsed = urlsplit(str(value or ''))
    except ValueError:
        return ''
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ''))


def source_identity(row):
    return json.dumps([row.get('url'), row.get('title'), row.get('published_date'), row.get('data_date'),
        row.get('period_end'), row.get('content') or row.get('snippet')], ensure_ascii=False, sort_keys=True, default=str)


def bind_source_ids(rows):
    """Rebind only missing/colliding IDs; originals remain explicit, possibly ambiguous aliases."""
    groups = {}
    for row in rows:
        for reference in [row.get('source_id'), *(row.get('source_id_aliases') or [])]:
            if reference:
                groups.setdefault(reference, set()).add(source_identity(row))
    reserved = set(groups)
    assigned = {}
    result = []
    for original in rows:
        row = deepcopy(original)
        old = row.get('source_id')
        identity = source_identity(row)
        if not old or len(groups[old]) > 1:
            if identity not in assigned:
                digest = hashlib.sha256(identity.encode()).hexdigest()
                candidate = 'S' + str(int(digest[:16], 16))
                while candidate in reserved:
                    candidate += str(int(digest[16:24], 16))
                assigned[identity] = candidate
                reserved.add(candidate)
            row['source_id'] = assigned[identity]
        aliases = list(dict.fromkeys([*(row.get('source_id_aliases') or []),
                                     *([old] if old and old != row['source_id'] else [])]))
        if aliases:
            row['source_id_aliases'] = aliases
        result.append(row)
    return result


def reference_sources(reference, sources, *, url='', published_date=None):
    """A date annotation disambiguates only an exact supplied ID/date pair."""
    reference = str(reference or '').strip()
    match = re.fullmatch(r'(S\d+)\s*[（(](\d{4}-\d{2}-\d{2})[）)]', reference)
    annotated = match.group(2) if match else None
    reference = match.group(1) if match else reference
    if url and (label := re.fullmatch(r'(S\d+)\s*[（(][^（）()]+[）)]', reference)):
        reference = label.group(1)
    if annotated and published_date and normalize_published_date(published_date) != annotated:
        return []
    day = annotated or normalize_published_date(published_date)
    result = []
    for row in sources:
        if reference not in [row.get('source_id'), *(row.get('source_id_aliases') or [])]:
            continue
        if url and source_url(row.get('url')) != source_url(url):
            continue
        if day and normalize_published_date(row.get('published_date')) != day:
            continue
        result.append(row)
    return result

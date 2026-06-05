import re
from urllib.parse import urlparse, parse_qs

ACESTREAM_HASH_RE = re.compile(r'^[0-9a-fA-F]{40}$')
_ATTR_RE = re.compile(r'([a-zA-Z0-9\-]+)="([^"]*)"')


def extract_infohash(url):
    """Pull a 40-char AceStream hash out of acestream://, http://…?id=, or a bare hash."""
    if not url:
        return None
    url = url.strip()

    if ACESTREAM_HASH_RE.match(url):
        return url.lower()

    if url.startswith('acestream://'):
        candidate = url[len('acestream://'):].strip().rstrip('/')
        if ACESTREAM_HASH_RE.match(candidate):
            return candidate.lower()
        return None

    parsed = urlparse(url)
    if parsed.scheme in ('http', 'https') and parsed.query:
        qs = parse_qs(parsed.query)
        for key in ('id', 'infohash', 'content_id'):
            if key in qs and qs[key]:
                candidate = qs[key][0]
                if ACESTREAM_HASH_RE.match(candidate):
                    return candidate.lower()
    return None


def parse_extinf_attrs(line):
    """Pull tvg-logo / tvg-id / tvg-name / group-title out of an #EXTINF line."""
    attrs = dict(_ATTR_RE.findall(line))
    display_name = ''
    if ',' in line:
        display_name = line.rsplit(',', 1)[1].strip()
    return {
        'logo': attrs.get('tvg-logo', ''),
        'tvgid': attrs.get('tvg-id', ''),
        'tvg': attrs.get('tvg-name', '') or display_name,
        'group': attrs.get('group-title', '') or 'Unknown',
        'name': display_name or attrs.get('tvg-name', 'Unknown Channel'),
    }


def _merge_extgrp(line, attrs):
    content = line[len('#EXTGRP:'):].strip()
    grp_attrs = dict(_ATTR_RE.findall(content))
    if 'group-logo' in grp_attrs:
        attrs['group_logo'] = grp_attrs['group-logo']
    if 'group-title' in grp_attrs and (not attrs.get('group') or attrs['group'] == 'Unknown'):
        attrs['group'] = grp_attrs['group-title']
    plain = _ATTR_RE.sub('', content).strip()
    if plain and (not attrs.get('group') or attrs['group'] == 'Unknown'):
        attrs['group'] = plain


def iter_extinf_entries(text):
    """Yield (attrs_dict, url_line) tuples from an M3U playlist body."""
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith('#EXTINF:'):
            attrs = parse_extinf_attrs(line)
            i += 1
            while i < len(lines) and (not lines[i].strip() or lines[i].strip().startswith('#')):
                stripped = lines[i].strip()
                if stripped.startswith('#EXTGRP:'):
                    _merge_extgrp(stripped, attrs)
                i += 1
            if i < len(lines):
                yield attrs, lines[i].strip()
                i += 1
                continue
        i += 1

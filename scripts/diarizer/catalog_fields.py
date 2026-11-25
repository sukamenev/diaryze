"""Metadata conversion to the consolidated transcript schema 1.0."""

from decimal import Decimal, ROUND_HALF_UP
import json
import math
import re

from .config import TRANSCRIPT_SCHEMA_VERSION
from .identification import split_markdown, status_from_markdown


# Validate and normalize the optional short summary.
def valid_summary(value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('summary must be a nonempty string or null')
    value = ' '.join(value.split())
    if len(value.split()) > 20:
        raise ValueError('summary exceeds 20 words')
    return value


# Validate the optional numeric spam score.
def valid_spam_score(value):
    if value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('spam_score must be a finite number from 0 to 1 or null')
    return value


# Convert milliseconds to the archive's fixed-precision seconds format.
def duration_seconds(milliseconds):
    if type(milliseconds) is not int or milliseconds <= 0:
        raise ValueError('duration_ms must be a positive integer')
    value = (Decimal(milliseconds) / 1000).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
    if value <= 0:
        raise ValueError('Audio duration is below the schema resolution')
    return value


# Read a legacy summary from the transcript body.
def body_summary(body):
    match = re.search(r'^##[ \t]+(?:Кратко|Краткое содержание|Summary)[ \t]*\r?\n'
                      r'([\s\S]*?)(?=^#{1,2}[ \t]+|\Z)', body, re.M | re.I)
    return ' '.join(match[1].split()) if match else None


# Extract and validate the summary supplied by the model.
def response_summary(raw):
    values = [line.strip().split(':', 1)[1].strip() for line in raw.splitlines()
              if line.strip().startswith('SUMMARY_JSON:')]
    if not values:
        return False, None
    if len(values) != 1:
        raise ValueError('Multiple SUMMARY_JSON lines')
    return True, valid_summary(json.loads(values[0]))


# Add missing root metadata while preserving existing YAML and transcript text.
def patch_catalog_fields(raw, people, orgs, *, summaries=None):
    """Preserve the body and existing YAML verbatim; add only root fields/version."""
    lines, end, meta = split_markdown(raw)
    if str(meta.get('schema_version')) not in {'1.0', '1.1', '1.2', '2.0'}:
        raise ValueError('Unsupported transcript schema')
    pending = {}
    if 'summary' in meta:
        valid_summary(meta['summary'])
    else:
        old = body_summary(b''.join(lines[end + 1:]).decode('utf-8'))
        supplied = (summaries or {}).get(meta.get('sha256'))
        if supplied:
            if old != supplied['source_summary']:
                raise ValueError('Source summary changed since conversion plan')
            summary = valid_summary(supplied['summary'])
        elif old and len(old.split()) <= 20:
            summary = valid_summary(old)
        else:
            summary = None
        pending['summary'] = summary
    if 'spam_score' in meta:
        valid_spam_score(meta['spam_score'])
    else:
        pending['spam_score'] = None
    if 'duration_ms' in meta:
        duration = duration_seconds(meta['duration_ms'])
        if 'duration_s' in meta:
            if Decimal(str(meta['duration_s'])) != duration:
                raise ValueError('Conflicting duration_s and duration_ms')
            raise ValueError('Both duration fields present; resolve explicitly')
        index = next((i for i in range(1, end) if lines[i].startswith(b'duration_ms:')), None)
        if index is None:
            raise ValueError('Missing root duration_ms line')
        line = lines[index]
        ending = b'\r\n' if line.endswith(b'\r\n') else b'\n'
        match = re.fullmatch(rb'duration_ms:[ \t]*[0-9]+([ \t]*(?:#.*)?)', line.removesuffix(ending))
        if not match:
            raise ValueError('Cannot safely convert duration_ms')
        lines[index] = b'duration_s: ' + str(duration).encode() + match[1] + ending
    else:
        value = meta.get('duration_s')
        if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
            raise ValueError('Missing positive duration_s')
    version = next((i for i in range(1, end) if lines[i].startswith(b'schema_version:')), None)
    if version is None:
        raise ValueError('Missing root schema_version line')
    ending = b'\r\n' if lines[version].endswith(b'\r\n') else b'\n'
    if meta.get('schema_version') != TRANSCRIPT_SCHEMA_VERSION:
        text = lines[version].removesuffix(ending)
        match = re.fullmatch(rb'(schema_version:[ \t]*)(?:"[^"]*"|\x27[^\x27]*\x27|[^# \t]+)([ \t]*(?:#.*)?)', text)
        if not match:
            raise ValueError('Cannot safely update schema_version')
        lines[version] = match[1] + b'"' + TRANSCRIPT_SCHEMA_VERSION.encode() + b'"' + match[2] + ending
    additions = []
    for key, value in pending.items():
        # JSON scalars are valid YAML, including explicit null.
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        additions.append(key.encode() + b': ' + encoded + ending)
    lines[version + 1:version + 1] = additions
    return b''.join(lines), meta.get('identification') or status_from_markdown(raw, people, orgs)

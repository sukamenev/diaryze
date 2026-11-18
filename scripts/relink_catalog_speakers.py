#!/usr/bin/env python3
"""Conservatively link unlinked speakers in Text/ MD files to matching entries in people.yaml / orgs.yaml."""

import argparse
from decimal import Decimal
import json
import re

import yaml

if __package__:
    from .diarizer.config import ROOT, TS_LINE_RE
    from .diarizer.identification import identification_status, split_markdown
    from .diarizer.transcript import MetadataDumper
    from .migrate_identification import migrate
else:
    from diarizer.config import ROOT, TS_LINE_RE
    from diarizer.identification import identification_status, split_markdown
    from diarizer.transcript import MetadataDumper
    from migrate_identification import migrate


NAME_SYNONYMS = {
    "анатолий": {"толя"}, "толя": {"анатолий"},
    "василиса": {"вася"}, "вася": {"василиса"},
    "елена": {"лена"}, "лена": {"елена"},
    "екатерина": {"катя"}, "катя": {"екатерина"},
    "константин": {"костя"}, "костя": {"константин"},
    "дмитрий": {"дима"}, "дима": {"дмитрий"},
    "александр": {"саша"}, "саша": {"александр"},
    "сергей": {"серёжа", "сережа"}, "серёжа": {"сергей"}, "сережа": {"сергей"},
    "юрий": {"юра"}, "юра": {"юрий"},
    "татьяна": {"таня"}, "таня": {"татьяна"},
    "ольга": {"оля"}, "оля": {"ольга"},
    "мария": {"маша"}, "маша": {"мария"},
    "анастасия": {"настя"}, "настя": {"анастасия"},
    "любовь": {"люба"}, "люба": {"любовь"},
    "владимир": {"вова"}, "вова": {"владимир"},
    "евгений": {"женя"}, "женя": {"евгений"},
    "михаил": {"миша"}, "миша": {"михаил"},
    "валентина": {"валя"}, "валя": {"валентина"},
    "виталий": {"виталик"}, "виталик": {"виталий"},
    "анна": {"аня"}, "аня": {"анна"},
    "виктор": {"витя"}, "витя": {"виктор"},
    "алексей": {"лёша", "леша"}, "лёша": {"алексей"}, "леша": {"алексей"},
    "наталья": {"наташа"}, "наташа": {"наталья"},
    "ирина": {"ира"}, "ира": {"ирина"},
    "максим": {"макс"}, "макс": {"максим"},
    "рустик": {"рустяха"}, "рустяха": {"рустик"},
}


def _phone_forms(raw: str) -> set[str]:
    digits = re.sub(r"\D+", "", str(raw or ""))
    if not digits:
        return set()
    forms = {digits}
    if len(digits) >= 10:
        forms.add(digits[-10:])
    return forms


def _tokens(text: str) -> set[str]:
    base = {w for w in re.findall(r"[а-яёa-z0-9]+", str(text or "").lower()) if len(w) >= 3}
    expanded = set(base)
    for w in base:
        expanded |= NAME_SYNONYMS.get(w, set())
    return expanded


def _find_person_match(meta: dict, people: dict) -> tuple[str, dict] | None:
    phones = set()
    for p in meta.get("phones") or []:
        phones |= _phone_forms(p)
    contact = str(meta.get("contact") or "").strip().lower()

    by_phone = []
    by_contact = []
    for pid, pinfo in people.items():
        if not isinstance(pinfo, dict):
            continue
        p_forms = set()
        for ep in pinfo.get("phones") or []:
            p_forms |= _phone_forms(ep)
        if phones and (phones & p_forms):
            by_phone.append((pid, pinfo))
        names = {str(pinfo.get("name") or "").strip().lower()} | {
            str(a).strip().lower() for a in (pinfo.get("aliases") or [])
        }
        names.discard("")
        if contact and contact in names:
            by_contact.append((pid, pinfo))

    if len(by_contact) == 1:
        pid, pinfo = by_contact[0]
        if not by_phone or any(p == pid for p, _ in by_phone):
            return pid, pinfo
        return None
    if len(by_phone) == 1 and not by_contact:
        return by_phone[0]
    return None


def _speaker_matches_entry(sp_key: str, info: dict, pinfo: dict, contact: str) -> bool:
    if info.get("temp") or info.get("role") in {"robot", "operator", "owner"}:
        return False
    sp_low = sp_key.strip().lower()
    lbl_low = str(info.get("label") or "").strip().lower()
    exact_names = {str(pinfo.get("name") or "").strip().lower()} | {
        str(a).strip().lower() for a in (pinfo.get("aliases") or [])
    }
    if contact:
        exact_names.add(contact.strip().lower())
    exact_names.discard("")
    if sp_low in exact_names or lbl_low in exact_names:
        return True

    entry_toks = _tokens(pinfo.get("name") or "")
    for a in pinfo.get("aliases") or []:
        entry_toks |= _tokens(a)
    if contact:
        entry_toks |= _tokens(contact)

    sp_toks = _tokens(sp_key) | _tokens(lbl_low)
    return bool(sp_toks and (sp_toks & entry_toks))


def relink_markdown_speakers(raw: bytes, people: dict, orgs: dict) -> tuple[bytes, str]:
    lines, end, meta = split_markdown(raw)
    if str(meta.get("schema_version")) not in {"1.0", "2.0"}:
        raise ValueError(f"Expected schema 1.0/2.0, got {meta.get('schema_version')}")
    body_text = b"".join(lines[end + 1:]).decode("utf-8")
    used = {(m.group("sp_b") or m.group("sp_p")).strip()
            for line in body_text.splitlines() if (m := TS_LINE_RE.match(line.strip()))}
    no_speech = not used and bool(re.search(r"(?m)^\[без речи\]\s*$", body_text))

    speakers = meta.get("speakers")
    if not isinstance(speakers, dict) or not speakers:
        return raw, identification_status(speakers, people, orgs, used_speakers=used, no_speech=no_speech)

    matched = _find_person_match(meta, people)
    if not matched:
        return raw, identification_status(speakers, people, orgs, used_speakers=used, no_speech=no_speech)
    pid, pinfo = matched

    # Do not assign if already linked to this person in this file
    if any(isinstance(v, dict) and v.get("person") == pid for v in speakers.values()):
        return raw, identification_status(speakers, people, orgs, used_speakers=used, no_speech=no_speech)

    contact = str(meta.get("contact") or "")
    candidates = [
        sp for sp, info in speakers.items()
        if isinstance(info, dict) and info.get("person") is None
        and _speaker_matches_entry(sp, info, pinfo, contact)
    ]
    if len(candidates) != 1:
        return raw, identification_status(speakers, people, orgs, used_speakers=used, no_speech=no_speech)

    target_sp = candidates[0]
    new_speakers = {}
    for sp, info in speakers.items():
        if sp != target_sp:
            new_speakers[sp] = info
            continue
        updated = dict(info)
        updated["person"] = pid
        if updated.get("source") not in {"speech", "user"}:
            updated["source"] = "filename"
        ordered = {}
        for k in ("person", "org", "role", "source", "temp", "label", "note"):
            if k in updated:
                ordered[k] = updated[k]
        for k, v in updated.items():
            if k not in ordered:
                ordered[k] = v
        new_speakers[sp] = ordered

    new_status = identification_status(new_speakers, people, orgs, used_speakers=used, no_speech=no_speech)
    new_meta = {}
    for k, v in meta.items():
        if k == "identification":
            new_meta[k] = new_status
        elif k == "duration_s" and isinstance(v, (int, float)):
            new_meta[k] = Decimal(str(v)).quantize(Decimal("0.01"))
        elif k == "speakers":
            new_meta[k] = new_speakers
        else:
            new_meta[k] = v

    nl_str = "\r\n" if lines and lines[0].endswith(b"\r\n") else "\n"
    yaml_text = yaml.dump(new_meta, Dumper=MetadataDumper, allow_unicode=True, sort_keys=False, width=1000).strip()
    yaml_text = yaml_text.replace("\n", nl_str)
    rendered = f"---{nl_str}{yaml_text}{nl_str}---{nl_str}{nl_str}" + body_text.lstrip("\r\n")
    return rendered.encode("utf-8"), new_status


def main():
    ap = argparse.ArgumentParser(description="Relink person: null speakers in Text/ MD files to people.yaml entries.")
    ap.add_argument("--folder", help="Optional subfolder under Text/")
    ap.add_argument("--apply", action="store_true", help="Write updated MD files")
    args = ap.parse_args()
    result = migrate(ROOT, folder=args.folder, apply=args.apply,
                     patcher=relink_markdown_speakers, journal_prefix="relink-catalog")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

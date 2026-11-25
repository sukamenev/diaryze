"""Render data into the English templates in the archive's promts/ directory."""

import json
from pathlib import Path
import re

from .archive import catalog_paths
from .prompt_files import load_prompt
from .owners import find_folder_owner, owner_label
from .transcript import format_ts


# Normalize contact numbers for matching prompt references.
def _phone_forms(phone: str) -> set[str]:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return set()
    forms = {digits}
    if len(digits) >= 10:
        forms.add(digits[-10:])
    return forms


# Collect searchable name, alias, and nickname tokens from a catalog card.
def _entry_tokens(info: dict) -> set[str]:
    texts = (
        [info.get("name") or ""]
        + list(info.get("aliases") or [])
        + list(info.get("nicknames") or [])
    )
    tokens = set()
    for text in texts:
        for word in re.findall(r"[а-яёa-z0-9]+", str(text).lower()):
            if len(word) >= 3:
                tokens.add(word)
    return tokens


# Check whether a catalog card is relevant to a recording.
def _matches_item(info: dict, item_phones: set[str], item_contacts: set[str], item_tokens: set[str]) -> bool:
    if not isinstance(info, dict):
        return False
    for phone in info.get("phones") or []:
        if _phone_forms(phone) & item_phones:
            return True
    names = (
        {str(info.get("name") or "").strip().lower()}
        | {str(a).strip().lower() for a in (info.get("aliases") or [])}
        | {str(n).strip().lower() for n in (info.get("nicknames") or [])}
    )
    names.discard("")
    if names & item_contacts:
        return True
    return bool(item_tokens and (_entry_tokens(info) & item_tokens))


# Select detailed cards and compact references for the prompt.
def _select_refs(people: dict, orgs: dict, items: list[dict] | None) -> dict:
    people_path, orgs_path = catalog_paths()
    if not items or (len(people) + len(orgs)) <= 25:
        return {
            "catalog_paths": {"people": people_path, "orgs": orgs_path},
            "people.yaml": people,
            "orgs.yaml": orgs,
        }

    item_phones: set[str] = set()
    item_contacts: set[str] = set()
    item_tokens: set[str] = set()
    owners = set()
    for item in items:
        folder, _ = find_folder_owner(item["audio_rel"]) if item.get("audio_rel") else (None, None)
        if folder and isinstance(folder.get("owner"), str):
            owners.add(folder["owner"])
        for p in item.get("phones") or []:
            item_phones |= _phone_forms(p)
        contact = str(item.get("contact") or "").strip().lower()
        if contact:
            item_contacts.add(contact)
            for word in re.findall(r"[а-яёa-z0-9]+", contact):
                if len(word) >= 3:
                    item_tokens.add(word)

    matched_people = {}
    other_people = {}
    for key, info in people.items():
        tags = set((info or {}).get("tags") or []) if isinstance(info, dict) else set()
        always = key in owners or bool(tags & {"owner", "family"})
        if always or _matches_item(info, item_phones, item_contacts, item_tokens):
            matched_people[key] = info
        elif isinstance(info, dict) and info.get("name"):
            nicks = [str(n).strip() for n in (info.get("nicknames") or []) if str(n).strip()]
            other_people[key] = f"{info['name']} [{', '.join(nicks)}]" if nicks else info["name"]

    matched_orgs = {}
    other_orgs = {}
    for key, info in orgs.items():
        if _matches_item(info, item_phones, item_contacts, item_tokens):
            matched_orgs[key] = info
        elif isinstance(info, dict) and info.get("name"):
            other_orgs[key] = info["name"]

    payload = {
        "catalog_paths": {"people": people_path, "orgs": orgs_path},
        "people.yaml": matched_people,
        "orgs.yaml": matched_orgs,
    }
    if other_people:
        payload["other_people_keys"] = other_people
    if other_orgs:
        payload["other_orgs_keys"] = other_orgs
    return payload


# Render the selected people and organization references.
def format_refs_for_prompt(people: dict, orgs: dict, items: list[dict] | None = None) -> str:
    return json.dumps(_select_refs(people, orgs, items), ensure_ascii=False, indent=2)


# Describe the recording type, phone owner, and any direction evidence.
def owner_hint(audio_rel: str, root: str | None = None, *, people=None, direction=None) -> str:
    meta, owner_rel = find_folder_owner(audio_rel, root=root)
    if meta:
        rec_type = str(meta.get("type") or "other").strip().lower()
        owner = meta.get("owner")
        if rec_type == "phone" and isinstance(owner, str) and (people is None or owner in people):
            label = owner_label(meta, people or {})
            return (
                f"Folder metadata `{owner_rel}`: type={rec_type}, usual owner is {owner}. "
                f"When the device user speaks (caller in outgoing, answerer in incoming) and the conversation does not indicate someone else is using the phone, "
                f"use speaker key `{label}` and person={owner} (with source='speech' if speech/context confirms it, "
                f"or source='filename' if inferred from folder owner/direction); never use generic keys like Звонящий/Абонент/Владелец телефона. "
                f"When only a robot/voicemail speaks, still include {label} as a silent call participant "
                f"with person={owner}, role='owner', source='filename', without invented utterances."
            )
        if rec_type == "recorder":
            return load_prompt("owner-other.md")
    if direction in {"in", "out"} or audio_rel.startswith("Phone/") or (meta and rec_type == "phone"):
        return load_prompt("owner-unknown-phone.md")
    return load_prompt("owner-other.md")


# Build the instructions and recording descriptors for short audio files.
def build_batch_prompt(items: list[dict], people: dict, orgs: dict) -> str:
    descriptions = []
    for index, item in enumerate(items, 1):
        descriptions.append('\n'.join((
            f'ITEM {index}:',
            f"- file: `{item['clean_abs']}`",
            f"- filename: `{Path(item['audio_rel']).name}`",
            f"- audio_rel: `{item['audio_rel']}`",
            f"- duration: {format_ts(item['duration_ms'])} ({item['duration_ms']} ms)",
            f"- contact: {item.get('contact')}",
            f"- phones: {item.get('phones', [])}",
            f"- context: {owner_hint(item['audio_rel'], people=people, direction=item.get('direction'))}",
        )))
    return load_prompt('batch.md', rules=load_prompt('rules.md'),
                       references=format_refs_for_prompt(people, orgs, items),
                       items='\n\n'.join(descriptions))


# Build a segment prompt with the context needed for its session.
def build_part_prompt(item: dict, part: dict, people: dict, orgs: dict, is_first: bool,
                      *, fresh_session=False, speaker_context=None) -> str:
    common = {'rules': load_prompt('rules.md'), 'path': part['path'],
              'part_duration': format_ts(part['duration_ms']),
              'part_duration_ms': part['duration_ms']}
    if is_first:
        return load_prompt('part-first.md', **common, audio_rel=item['audio_rel'],
                           total_duration=format_ts(item['duration_ms']),
                           references=format_refs_for_prompt(people, orgs, [item]),
                           owner_hint=owner_hint(item['audio_rel'], people=people, direction=item.get('direction')))
    prompt = load_prompt('part-next.md', **common, part_number=part['part'],
                         overlap_seconds=part.get('overlap_ms', 15000) / 1000,
                         previous_summary=json.dumps(item.get('summary'), ensure_ascii=False))
    if fresh_session:
        return load_prompt('part-context.md', part_prompt=prompt, audio_rel=item['audio_rel'],
                           references=format_refs_for_prompt(people, orgs, [item]),
                           owner_hint=owner_hint(item['audio_rel'], people=people, direction=item.get('direction')),
                           speakers=json.dumps(speaker_context or {}, ensure_ascii=False))
    return prompt

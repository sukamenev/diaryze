"""Folder ownership and silent participants in phone calls answered by robots."""

from pathlib import Path

import yaml

from . import config


def find_folder_owner(audio_rel: str, root: str | None = None) -> tuple[dict | None, str | None]:
    """Read the nearest .owner, respecting recorder/unknown-owner overrides."""
    root_path = Path(root or config.ROOT).resolve()
    rel_dir = (root_path / audio_rel).resolve().parent
    if not rel_dir.is_relative_to(root_path):
        return None, None
    cur = rel_dir
    while True:
        candidate = cur / ".owner"
        if candidate.is_file():
            data = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data, candidate.relative_to(root_path).as_posix()
        if cur == root_path:
            break
        cur = cur.parent
    return None, None


def owner_label(folder, people):
    owner = folder["owner"]
    entry = people.get(owner) or {}
    return str(folder.get("label") or entry.get("name") or owner)


def with_silent_phone_owner(speakers, audio_rel, people, *, used_speakers, direction, root=None):
    """Include the known device owner when only robots speak; never invent utterances.

    Human voices need their own identity assessment: an absent owner there can mean
    someone else used the phone. This fallback only handles robot-only phone calls.
    """
    if not isinstance(speakers, dict) or not speakers:
        return speakers
    used = set(used_speakers)
    if not used or not used.issubset(speakers):
        return speakers
    if direction not in {"in", "out", "incoming", "outgoing"}:
        return speakers
    if not all(isinstance(info, dict) and info.get("role") == "robot"
               and not info.get("person") for info in speakers.values()):
        return speakers
    folder, _ = find_folder_owner(audio_rel, root=root)
    if not folder or folder.get("type") != "phone":
        return speakers
    owner = folder.get("owner")
    if not isinstance(owner, str) or owner not in people:
        return speakers
    label = owner_label(folder, people)
    key = label
    suffix = 2
    while key in speakers:
        key = f"{label} {suffix}"
        suffix += 1
    return dict(speakers, **{key: {
        "person": owner,
        "role": "owner",
        "source": "filename",
        "note": "Владелец телефона; без слышимых реплик",
    }})

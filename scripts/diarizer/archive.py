"""Audio discovery, metadata, catalogs, manifest and result publication."""

from datetime import datetime
import hashlib
import json
import os
import re
import threading
import yaml

from . import config
from .config import AUDIO_EXTENSIONS, EXCLUDED_DIRS, EXCLUDED_FILES, NEEDS_RECOVERY, NO_RECOVERABLE_AUDIO, TRANSCRIPT_SCHEMA_VERSION, TZ


MANIFEST_LOCK = threading.Lock()
CATALOG_LOCK = threading.Lock()

RESERVED_CATALOG_KEYS = {
    "unknown", "null", "none", "anonymous", "caller", "callee", "operator", "robot",
    "sobesednik", "speaker", "voice", "golos", "abonent", "zvonyashhij", "vladelec",
    "klient", "kurer", "neizvestno", "neizvestnyj", "muzhchina", "zhenshhina",
}
GENERIC_DISPLAY_NAMES = {
    "", "?", "unknown", "неизвестный", "неизвестная", "неизвестно", "звонящий",
    "абонент", "владелец", "владелец телефона", "ответивший", "собеседник",
    "собеседница", "оператор", "робот", "автоответчик", "автоинформатор",
    "курьер", "клиент", "мужчина", "женщина", "голос", "голосовой секретарь",
}


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("8") and len(digits) == 11:
        return "+7" + digits[1:]
    if digits.startswith("7") and len(digits) == 11:
        return "+" + digits
    return digits


_DIRECTION_MAP = {"incoming": "in", "outgoing": "out", "in": "in", "out": "out"}


def parse_filename(fn: str):
    m = re.match(
        r"^(?P<contact>.+?)_(?P<phone>[^_]+)_(?P<date>\d{4}-\d{2}-\d{2})_"
        r"(?P<time>\d{2}-\d{2}-\d{2})_(?P<dir>incoming|outgoing|unknown)(?:_\(\d+\))?(?:\.[^.]*)?$",
        fn,
    )
    if not m:
        return parse_other_filename(fn)
    dt = f"{m.group('date')}T{m.group('time').replace('-', ':')}"
    try:
        datetime.fromisoformat(dt)
    except ValueError:
        return None, None, None, []
    direction = _DIRECTION_MAP.get(m.group("dir"))
    phone = normalize_phone(m.group("phone"))
    raw_contact = m.group("contact").strip()
    contact = (
        None
        if normalize_phone(raw_contact) == phone or re.fullmatch(r"[\d\s()+-]+", raw_contact)
        else raw_contact
    )
    phones = [phone] if phone else []
    return dt, direction, contact, phones


def parse_other_filename(fn):
    """Recorder/MIUI, WhatsApp dates and Android epoch names; never use mtime as date."""
    vrf = re.fullmatch(r"[01]d(\d{14})p(.+?)(?:_(incoming|outgoing))?\.[^.]+", fn)
    if vrf:
        try:
            dt = datetime.strptime(vrf[1], "%Y%m%d%H%M%S").isoformat()
        except ValueError:
            return None, None, None, []
        return dt, _DIRECTION_MAP.get(vrf[3]), None, [normalize_phone(vrf[2])]
    miui = re.fullmatch(r"(?:Запись вызовов )?(.+?)_(\d{6})_(\d{6})(?:\.[^.]*)?", fn)
    if miui:
        try:
            dt = datetime.strptime(miui[2] + miui[3], "%y%m%d%H%M%S").isoformat()
        except ValueError:
            return None, None, None, []
        contact = miui[1].strip()
        if re.fullmatch(r"[\d\s()+-]+", contact):
            return dt, None, None, [normalize_phone(contact)]
        return dt, None, contact, []
    epoch = re.fullmatch(r"(\d{10})-[01]-(.+)\.[^.]+", fn)
    if epoch:
        dt = datetime.fromtimestamp(int(epoch[1]), TZ).isoformat()
        return dt, None, None, [normalize_phone(epoch[2])]
    recording = re.match(r"(?:recording-)?(\d{8})[-_](\d{6})(?:\D|$)", fn)
    day = re.match(r"(?:(?:AUD|PTT|VID)-)?(\d{8})(?:[-_.]|$)", fn)
    try:
        if recording:
            return datetime.strptime(recording[1] + recording[2], "%Y%m%d%H%M%S").isoformat(), None, None, []
        if day:
            return datetime.strptime(day[1], "%Y%m%d").date().isoformat(), None, None, []
    except ValueError:
        pass
    return None, None, None, []


def resolve_source_dir(folder):
    root = os.path.realpath(config.ROOT)
    path = os.path.realpath(os.path.join(root, os.path.expanduser(folder)))
    if os.path.commonpath([root, path]) != root:
        raise ValueError("Папка должна находиться внутри " + config.ROOT)
    rel = os.path.relpath(path, root)
    if any(is_excluded_dir(part) for part in rel.split(os.sep)):
        raise ValueError("Служебная папка исключена из поиска: " + folder)
    if not os.path.isdir(path):
        raise ValueError("Папка не найдена: " + folder)
    return path


def is_audio_candidate(path):
    """Extensions plus common signatures; ffprobe later verifies an audio stream."""
    ext = os.path.splitext(path)[1].lower()
    if ext in AUDIO_EXTENSIONS or ext in {"", ".tmp", ".crtemp"}:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(64)
    except OSError:
        return False
    return (head.startswith((b"ID3", b"OggS", b"fLaC", b"#!AMR", b"FORM", b"\x1aE\xdf\xa3"))
            or (head.startswith(b"RIFF") and head[8:12] in {b"WAVE", b"AVI "})
            or (len(head) > 1 and head[0] == 255 and head[1] & 0xe0 == 0xe0)
            or head[4:8] in {b"ftyp", b"mdat"})


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_excluded_dir(name: str) -> bool:
    low = name.lower()
    return low in EXCLUDED_DIRS or "trash" in low or "корзин" in low


def load_manifest_state():
    """Return latest status map {audio_rel: entry} from manifest.jsonl."""
    state = {}
    manifest = os.path.join(config.ROOT, "docs", "manifest.jsonl")
    if not os.path.exists(manifest):
        return state
    with MANIFEST_LOCK, open(manifest, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if "audio" in rec:
                    state[rec["audio"]] = rec
            except json.JSONDecodeError:
                continue
    return state


def append_manifest(entry: dict):
    manifest = os.path.join(config.ROOT, "docs", "manifest.jsonl")
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
    with MANIFEST_LOCK, open(manifest, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def catalog_paths():
    directory = os.path.join(config.ROOT, "docs")
    return os.path.join(directory, "people.yaml"), os.path.join(directory, "orgs.yaml")


def _load_catalog(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict) or not isinstance(data.get(key), dict):
        raise ValueError(f"Expected a YAML mapping named {key!r} in {path}")
    return data[key]


def load_refs():
    people_path, orgs_path = catalog_paths()
    return _load_catalog(people_path, "people"), _load_catalog(orgs_path, "orgs")


def init_catalogs():
    """Seed empty catalogs while preserving files the user already has."""
    with CATALOG_LOCK:
        for path, key in zip(catalog_paths(), ("people", "orgs")):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            try:
                with open(path, "x", encoding="utf-8") as f:
                    yaml.safe_dump({"schema_version": "1.0", key: {}}, f, sort_keys=False)
            except FileExistsError:
                continue


def _phone_forms(phone: str) -> set[str]:
    digits = re.sub(r"\D+", "", str(phone or ""))
    if not digits:
        return set()
    forms = {digits}
    if len(digits) >= 10:
        forms.add(digits[-10:])
    return forms


def _valid_new_catalog_key(key: str) -> bool:
    return (
        isinstance(key, str)
        and bool(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", key))
        and key not in RESERVED_CATALOG_KEYS
        and not re.fullmatch(r"(?:s|golos|voice|speaker|temp)_?\d+", key)
    )


def _find_existing_catalog_match(catalog: dict, phones: list[str], names: set[str]) -> str | None:
    pforms = set()
    for p in phones:
        pforms |= _phone_forms(p)
    for k, entry in catalog.items():
        if not isinstance(entry, dict):
            continue
        if pforms:
            for ep in entry.get("phones") or []:
                if _phone_forms(ep) & pforms:
                    return k
        en = {str(entry.get("name") or "").strip().lower()} | {
            str(a).strip().lower() for a in (entry.get("aliases") or [])
        }
        en.discard("")
        if names and (en & names):
            return k
    return None


def _write_catalog_yaml(path: str, root_key: str, data: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump({"schema_version": "1.0", root_key: data}, f, allow_unicode=True, sort_keys=False, width=1000)
    os.replace(tmp, path)


def register_catalog_entries(speakers: dict, item: dict, people: dict, orgs: dict):
    """Thread-safely register valid new GOST person/org IDs into people.yaml / orgs.yaml."""
    if not isinstance(speakers, dict):
        return
    p_path, o_path = catalog_paths()
    contact = str(item.get("contact") or "").strip()
    item_phones = [str(p) for p in (item.get("phones") or []) if p]

    with CATALOG_LOCK:
        # Workers may have added entries while this request was in flight.
        people.update(_load_catalog(p_path, "people"))
        orgs.update(_load_catalog(o_path, "orgs"))

        people_dirty = False
        orgs_dirty = False
        all_used_phones = set()
        for cat in (people, orgs):
            for entry in cat.values():
                if isinstance(entry, dict):
                    for ep in entry.get("phones") or []:
                        all_used_phones |= _phone_forms(ep)

        for sp_key, info in speakers.items():
            if not isinstance(info, dict):
                continue
            source = info.get("source") or info.get("basis")
            if source not in {"filename", "speech", "audio", "user"}:
                continue

            pid = info.get("person")
            if not info.get("temp") and isinstance(pid, str) and pid != "unknown" and pid not in people and _valid_new_catalog_key(pid):
                lbl = str(info.get("label") or "").strip()
                sp_clean = str(sp_key).strip()
                # Prefer filename contact if label matches contact or speaker looks like it matches filename contact
                cand_name = lbl if lbl.lower() not in GENERIC_DISPLAY_NAMES else ""
                if not cand_name and contact and contact.lower() not in GENERIC_DISPLAY_NAMES:
                    c_tokens = set(re.findall(r"[а-яёa-z0-9]+", contact.lower()))
                    s_tokens = set(re.findall(r"[а-яёa-z0-9]+", sp_clean.lower()))
                    if (s_tokens & c_tokens) or source == "filename":
                        cand_name = contact
                if not cand_name and sp_clean.lower() not in GENERIC_DISPLAY_NAMES and not re.fullmatch(r"(?:S|Голос\s*)\d+", sp_clean):
                    cand_name = sp_clean

                if cand_name:
                    names_for_match = {cand_name.lower()}
                    if contact and contact.lower() not in GENERIC_DISPLAY_NAMES:
                        c_low = contact.lower()
                        if any(tok in c_low for tok in re.findall(r"[а-яёa-z0-9]{3,}", cand_name.lower())):
                            names_for_match.add(c_low)
                    existing = _find_existing_catalog_match(
                        people,
                        item_phones if (contact and contact.lower() in names_for_match) else [],
                        names_for_match,
                    )
                    if existing:
                        info["person"] = existing
                    else:
                        new_entry = {"name": cand_name}
                        attach_phones = [
                            p for p in item_phones
                            if contact and contact.lower() in names_for_match and not (_phone_forms(p) & all_used_phones)
                        ]
                        if attach_phones:
                            new_entry["phones"] = attach_phones
                            for p in attach_phones:
                                all_used_phones |= _phone_forms(p)
                        if contact and contact != cand_name and contact.lower() in names_for_match:
                            new_entry["aliases"] = [contact]
                        new_entry["notes"] = f"Добавлено автоматически из {item.get('audio_rel')}."
                        people[pid] = new_entry
                        people_dirty = True

            oid = info.get("org")
            if info.get("role") == "robot" and isinstance(oid, str) and oid not in orgs and _valid_new_catalog_key(oid):
                lbl = str(info.get("label") or "").strip()
                c_non_phone = contact if (contact and contact.lower() not in GENERIC_DISPLAY_NAMES and re.search(r"[а-яёa-z]", contact, re.I)) else ""
                cand_org = lbl if lbl.lower() not in GENERIC_DISPLAY_NAMES else c_non_phone
                names_for_org = {cand_org.lower()} if cand_org else set()
                existing_org = _find_existing_catalog_match(orgs, item_phones, names_for_org)
                if existing_org:
                    info["org"] = existing_org
                elif cand_org:
                    new_org = {"name": cand_org, "tags": ["service"]}
                    attach_phones = [
                        p for p in item_phones
                        if not (_phone_forms(p) & all_used_phones)
                        and ((c_non_phone and c_non_phone.lower() == cand_org.lower()) or bool(re.fullmatch(r"\+?[78]800\d{7}|\d{3,5}", p)))
                    ]
                    if attach_phones:
                        new_org["phones"] = attach_phones
                        for p in attach_phones:
                            all_used_phones |= _phone_forms(p)
                    new_org["notes"] = f"Добавлено автоматически из {item.get('audio_rel')}."
                    orgs[oid] = new_org
                    orgs_dirty = True

        if people_dirty:
            _write_catalog_yaml(p_path, "people", people)
        if orgs_dirty:
            _write_catalog_yaml(o_path, "orgs", orgs)


def md_has_matching_sha(md_path: str, audio_sha: str) -> bool:
    try:
        with open(md_path, encoding="utf-8") as f:
            head = f.read(4096)
        parts = head.split("---", 2)
        if len(parts) < 2:
            return False
        meta = yaml.safe_load(parts[1]) or {}
        return isinstance(meta, dict) and meta.get("sha256") == audio_sha
    except Exception:
        return False


def scan_pending(subdir: str = ".", retry_skipped: bool = False, retry_refused: bool = False,
                 *, retry_recovery: bool = False):
    """Scan subdir newest-first for audio files without a matching MD or terminal status."""
    src_root = resolve_source_dir(subdir)
    mstate = load_manifest_state()
    cands = []
    for dirpath, dirnames, filenames in os.walk(src_root):
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)
                      and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            if fn.lower() in EXCLUDED_FILES:
                continue
            fp = os.path.join(dirpath, fn)
            if os.path.islink(fp) or not os.path.isfile(fp) or not is_audio_candidate(fp):
                continue
            rel = os.path.relpath(fp, config.ROOT)
            prev = mstate.get(rel)
            st = (prev or {}).get("status")
            if st == "skipped" and not retry_skipped:
                reason = prev.get("reason")
                if not (retry_recovery and reason and reason not in NO_RECOVERABLE_AUDIO):
                    continue
            if st == NEEDS_RECOVERY and not retry_recovery:
                previous_sha = prev.get("sha256")
                if not previous_sha or sha256_file(fp) == previous_sha:
                    continue
            if st == "refused" and not retry_refused:
                continue
            md_path = os.path.join(config.ROOT, "Text", rel + ".md")
            if os.path.exists(md_path) and prev and prev.get("status") == "done":
                # Fast path: assume matching unless we want deep rehash; check sha if needed
                real_sha = sha256_file(fp)
                if prev.get("sha256") == real_sha and md_has_matching_sha(md_path, real_sha):
                    continue
            elif os.path.exists(md_path):
                real_sha = sha256_file(fp)
                if md_has_matching_sha(md_path, real_sha):
                    continue
            dt, direction, contact, phones = parse_filename(fn)
            try:
                mtime = os.path.getmtime(fp)
            except OSError:
                mtime = 0.0
            cands.append({
                "audio_rel": rel,
                "audio_abs": fp,
                "target_md": md_path,
                "dt": dt,
                "direction": direction,
                "contact": contact,
                "phones": phones,
                "mtime": mtime,
            })
    cands.sort(key=lambda x: (x["dt"] or "", x["mtime"]), reverse=True)
    return cands


def recorded_mtime_ns(recorded_at, *, audio_rel: str | None = None) -> int | None:
    """Return nanosecond Unix timestamp from recorded_at (or filename fallback), or None."""
    raw = str(recorded_at or "").strip()
    if not raw and audio_rel:
        fn_dt, *_ = parse_filename(os.path.basename(audio_rel))
        raw = str(fn_dt or "").strip()
    if not raw:
        return None
    try:
        if "T" in raw or " " in raw:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw + "T00:00:00")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return round(dt.timestamp() * 1_000_000_000)
    except (ValueError, TypeError, OverflowError):
        return None


def sync_file_mtime(path: str | os.PathLike, recorded_at, *, audio_rel: str | None = None, fallback_ns: int | None = None) -> int | None:
    """Set filesystem mtime from recorded_at (or fallback_ns), preserving atime."""
    target_ns = recorded_mtime_ns(recorded_at, audio_rel=audio_rel)
    if target_ns is None:
        target_ns = fallback_ns
    if target_ns is None:
        return None
    st = os.stat(path, follow_symlinks=False)
    os.utime(path, ns=(st.st_atime_ns, target_ns), follow_symlinks=False)
    return target_ns


def publish_md(item: dict, md_content: str, model: str):
    meta = yaml.safe_load(md_content.split("---", 2)[1]) or {}
    target = item["target_md"]
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md_content)
    os.replace(tmp, target)
    sync_file_mtime(target, meta.get("recorded_at"), audio_rel=item.get("audio_rel"))
    append_manifest({
        "audio": item["audio_rel"],
        "text": os.path.relpath(target, config.ROOT),
        "sha256": item["sha256"],
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "identification": meta["identification"],
        "status": "done",
        "model": model,
        "created_at": datetime.now(TZ).isoformat(timespec="seconds"),
    })
    return meta

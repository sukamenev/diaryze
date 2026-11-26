"""Edit existing transcript metadata while preserving the rest of the file."""

import json
import os
from pathlib import Path
import stat
import tempfile

import yaml
from yaml.nodes import MappingNode, ScalarNode
from yaml.tokens import FlowEntryToken

from .identification import split_markdown
from .platform import locked_path


# Write updated Markdown atomically and preserve file metadata.
def replace_markdown(path, before, raw, changed):
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".transcript-",
                                         suffix=".tmp", delete=False) as file:
            temp = Path(file.name)
            file.write(changed)
            file.flush()
            os.fsync(file.fileno())
        temp.chmod(stat.S_IMODE(before.st_mode))
        os.utime(temp, ns=(before.st_atime_ns, before.st_mtime_ns))
        if path.read_bytes() != raw:
            raise ValueError("Markdown changed since it was read")
        current = path.lstat()
        if (not os.path.samestat(current, before) or current.st_ctime_ns != before.st_ctime_ns
                or current.st_mtime_ns != before.st_mtime_ns):
            raise ValueError("Markdown changed since it was read")
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


# Edit one scratch mapping inside an inline YAML root without reformatting its neighbors.
def _patch_inline_root(header, root, entry, scratch):
    value = yaml.safe_dump(scratch, allow_unicode=True, sort_keys=False,
                           default_flow_style=True).strip()
    if entry:
        key, before = entry
        start, end = before.start_mark.index, before.end_mark.index
        if start < key.end_mark.index:
            raise ValueError("Cannot edit scratch through an alias")
        separator = " " if header[start - 1] == ":" else ""
        return header[:start] + separator + value + header[end:]
    end = root.end_mark.index - 1
    previous = [token for token in yaml.scan(header) if token.start_mark.index < end]
    separator = "" if not root.value or isinstance(previous[-1], FlowEntryToken) else ", "
    return header[:end] + separator + "scratch: " + value + header[end:]


# Read the optional scratch mapping with consistent validation.
def _scratch_mapping(meta):
    previous = meta.get("scratch")
    if previous is not None and (not isinstance(previous, dict) or
                                 any(not isinstance(key, str) for key in previous)):
        raise ValueError("scratch must be a mapping of script names to values")
    return dict(previous or {})


# Change one script's optional scratch value while preserving unrelated front matter and text.
def patch_scratch(raw, namespace, value=...):
    if not isinstance(namespace, str) or not namespace.strip():
        raise ValueError("Expected a nonempty scratch namespace")
    lines, end, meta = split_markdown(raw)
    scratch = _scratch_mapping(meta)
    if value is ...:
        if namespace not in scratch:
            return raw
        del scratch[namespace]
    else:
        value = json.loads(json.dumps(value, allow_nan=False))
        if namespace in scratch and yaml.safe_dump(scratch[namespace]) == yaml.safe_dump(value):
            return raw
        scratch[namespace] = value

    header = b"".join(lines[1:end]).decode("utf-8")
    root = yaml.compose(header, Loader=yaml.SafeLoader)
    if not isinstance(root, MappingNode):
        raise ValueError("Expected a YAML mapping in the Markdown header")
    entries = [(i, entry) for i, entry in enumerate(root.value)
               if isinstance(entry[0], ScalarNode) and entry[0].value == "scratch"]
    if len(entries) > 1:
        raise ValueError("Duplicate scratch field")
    newline = "\r\n" if lines[0].endswith(b"\r\n") else "\n"

    if root.flow_style:
        entry = entries[0][1] if entries else None
        revised = _patch_inline_root(header, root, entry, scratch).encode("utf-8")
        changed = lines[0] + revised + b"".join(lines[end:])
    else:
        start = stop = root.end_mark.line + 1
        indent = " " * root.start_mark.column
        comment = (indent + "#").encode("utf-8")
        if entries:
            index, (key, _) = entries[0]
            start = key.start_mark.line + 1
            if index + 1 < len(root.value):
                stop = root.value[index + 1][0].start_mark.line + 1
            while stop > start + 1 and (not lines[stop - 1].strip() or lines[stop - 1].startswith(comment)):
                stop -= 1
        rendered = yaml.safe_dump({"scratch": scratch}, allow_unicode=True, sort_keys=False)
        revised = "".join(indent + line + newline for line in rendered.split("\n")[:-1]).encode("utf-8")
        changed = b"".join(lines[:start]) + revised + b"".join(lines[stop:])

    _, _, actual = split_markdown(changed)
    if actual != dict(meta, scratch=scratch):
        raise ValueError("Cannot edit scratch without changing unrelated metadata")
    return changed


class TranscriptFile:
    """Read and update one transcript Markdown file."""

    # Bind the high-level metadata operations to a file path.
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # Read metadata and the transcript body together from one snapshot.
    def read(self) -> tuple[dict, str]:
        lines, end, metadata = split_markdown(self.path.read_bytes())
        return metadata, b"".join(lines[end + 1:]).decode("utf-8", errors="replace")

    # Read the current YAML metadata from disk.
    def read_metadata(self) -> dict:
        return split_markdown(self.path.read_bytes())[2]

    # Read one script's scratch value or the caller's default if it is absent.
    def get_scratch(self, namespace, default=None):
        return _scratch_mapping(self.read_metadata()).get(namespace, default)

    # Store a JSON-compatible value for one script and report whether it changed.
    def set_scratch(self, namespace, value) -> bool:
        return self._update_scratch(namespace, value)

    # Remove one script's scratch entry and report whether it was present.
    def clear_scratch(self, namespace) -> bool:
        return self._update_scratch(namespace, ...)

    # Apply the edit against the latest inode so concurrent writers retain each other's keys.
    def _update_scratch(self, namespace, value):
        while True:
            before = self.path.lstat()
            if self.path.suffix.lower() != ".md" or not stat.S_ISREG(before.st_mode):
                raise ValueError("Choose a regular .md file")
            with locked_path(self.path) as stream:
                before = self.path.lstat()
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError("Choose a regular .md file")
                if stream is not None and not os.path.samestat(before, os.fstat(stream.fileno())):
                    continue
                # Windows requires the target file to be closed before os.replace().
                raw = stream.read() if stream is not None else self.path.read_bytes()
                changed = patch_scratch(raw, namespace, value)
                if changed == raw:
                    return False
                replace_markdown(self.path, before, raw, changed)
                return True

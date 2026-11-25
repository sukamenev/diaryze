"""Audio probing, container/AAC recovery, remuxing and slicing."""

import ctypes
from ctypes.util import find_library
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import threading

from .archive import sha256_file
from .processes import run_capture


# Read the duration of a source that contains an audio stream.
def probe_duration_ms(path: str):
    p = run_capture(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "format=duration:stream=index,duration", "-of", "json", path],
        capture_output=True, text=True,
    )
    if p.returncode != 0 or not p.stdout.strip():
        err = p.stderr.strip().splitlines()[0] if p.stderr.strip() else "ffprobe failed"
        return None, err
    try:
        data = json.loads(p.stdout)
        streams = data.get("streams") or []
        if not streams:
            return None, "no_audio_stream"
        for duration in ((data.get("format") or {}).get("duration"), streams[0].get("duration")):
            try:
                ms = int(round(float(duration) * 1000))
            except (ValueError, TypeError, OverflowError):
                continue
            if ms > 0:
                return ms, None
    except (ValueError, TypeError, AttributeError):
        return None, "bad ffprobe response"
    return None, "no_positive_audio_duration"


# Find the optional container-repair tool on the executable path.
def find_untrunc() -> str | None:
    """Locate `untrunc` binary in PATH for moov-less MP4/3GP/M4A recovery."""
    return shutil.which("untrunc")


# Return the diagnostic from a strict full-stream audio decode.
def decode_audio_error(path: str) -> str | None:
    """Decode the whole stream strictly; a readable container alone is insufficient."""
    p = run_capture(
        ["ffmpeg", "-v", "error", "-xerror", "-abort_on", "empty_output",
         "-i", path, "-map", "0:a:0", "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if p.returncode != 0:
        return "audio decode failed: " + (p.stderr.strip() or f"ffmpeg exit {p.returncode}")[:300]
    return None


# Find nearby readable media suitable as a recovery reference.
def _find_reference_audio(fp: str, limit: int = 2) -> list[str]:
    """Pick nearby valid .m4a/.mp4 reference files in the same directory."""
    parent = os.path.dirname(fp)
    base = os.path.basename(fp)
    want_dir = "incoming" if "_incoming" in base else ("outgoing" if "_outgoing" in base else "")
    scored = []
    try:
        entries = os.scandir(parent)
    except OSError:
        return []
    for ent in entries:
        if not ent.is_file() or ent.path == fp:
            continue
        ext = os.path.splitext(ent.name)[1].lower()
        if ext not in {".m4a", ".mp4"}:
            continue
        try:
            sz = ent.stat().st_size
        except OSError:
            continue
        if sz < 4096:
            continue
        dir_score = 0 if (not want_dir or f"_{want_dir}" in ent.name) else 1
        scored.append((dir_score, ent.name, ent.path))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=False)
    refs = []
    for _, _, cand_path in scored[:12]:
        ms, err = probe_duration_ms(cand_path)
        if not err and ms and ms > 1000:
            refs.append(cand_path)
            if len(refs) >= limit:
                break
    return refs


# Try to rebuild the damaged container using a nearby reference recording.
def untrunc_recover(fp: str, tmp_dir: str, untrunc_bin: str | None) -> tuple[str | None, int | None]:
    """Attempt non-destructive recovery of moov-less m4a/3gp container into tmp_dir."""
    if not untrunc_bin:
        return None, None
    refs = _find_reference_audio(fp, limit=2)
    if not refs:
        return None, None

    src_for_untrunc = fp
    try:
        with open(fp, "rb") as f:
            head = f.read(64)
        # Android AudioRecorder .tmp files leave 64-bit mdat size as '????????'.
        mdat_idx = head.find(b"\x00\x00\x00\x01mdat????????")
        if mdat_idx != -1:
            patched_in = os.path.join(tmp_dir, "untrunc_in.mp4")
            shutil.copyfile(fp, patched_in)
            total_sz = os.path.getsize(patched_in)
            if total_sz > mdat_idx:
                with open(patched_in, "r+b") as pf:
                    pf.seek(mdat_idx + 8)
                    pf.write(struct.pack(">Q", total_sz - mdat_idx))
            src_for_untrunc = patched_in
    except OSError:
        src_for_untrunc = fp

    for ridx, ref in enumerate(refs):
        out_m4a = os.path.join(tmp_dir, f"untrunc_out_{ridx}.m4a")
        try:
            run_capture(
                [untrunc_bin, "-n", "-s", "-q", "-dst", out_m4a, ref, src_for_untrunc],
                capture_output=True, text=True, timeout=120,
            )
        except subprocess.TimeoutExpired:
            continue
        if os.path.exists(out_m4a) and os.path.getsize(out_m4a) > 1024:
            ms, err = probe_duration_ms(out_m4a)
            if not err and ms and ms > 0 and not decode_audio_error(out_m4a):
                return out_m4a, ms
    return None, None


class _FaadFrameInfo(ctypes.Structure):
    _fields_ = [
        ("bytesconsumed", ctypes.c_ulong),
        ("samples", ctypes.c_ulong),
        ("channels", ctypes.c_ubyte),
        ("error", ctypes.c_ubyte),
        ("samplerate", ctypes.c_ulong),
        ("sbr", ctypes.c_ubyte),
        ("object_type", ctypes.c_ubyte),
        ("header_type", ctypes.c_ubyte),
        ("num_front_channels", ctypes.c_ubyte),
        ("num_side_channels", ctypes.c_ubyte),
        ("num_back_channels", ctypes.c_ubyte),
        ("num_lfe_channels", ctypes.c_ubyte),
        ("channel_position", ctypes.c_ubyte * 64),
        ("ps", ctypes.c_ubyte),
        ("is_mps", ctypes.c_ubyte),
    ]


_FAAD_LOCK = threading.Lock()


_FAAD_LIB = [None, False]  # [lib_or_none, attempted]


# List possible FAAD2 library locations for the current platform.
def _faad_library_names():
    if sys.platform != "darwin":
        yield from ("libfaad.so.2", "libfaad.so")
        return
    names = ("libfaad.2.dylib", "libfaad.dylib")
    for prefix in ("/opt/homebrew", "/usr/local"):
        for name in names:
            path = Path(prefix) / "opt" / "faad2" / "lib" / name
            if path.is_file():
                yield str(path)
    if brew := shutil.which("brew"):
        try:
            result = subprocess.run([brew, "--prefix", "faad2"], capture_output=True,
                                    text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result and result.returncode == 0 and result.stdout.strip():
            for name in names:
                yield str(Path(result.stdout.strip()) / "lib" / name)
    if library := find_library("faad"):
        yield library
    yield from names


# Load and cache the FAAD2 decoder with its native function signatures.
def _get_libfaad():
    """Lazily load the FAAD2 shared library with ctypes signatures."""
    if _FAAD_LIB[1]:
        return _FAAD_LIB[0]
    with _FAAD_LOCK:
        if _FAAD_LIB[1]:
            return _FAAD_LIB[0]
        _FAAD_LIB[1] = True
        for name in _faad_library_names():
            try:
                lib = ctypes.CDLL(name)
                lib.NeAACDecOpen.restype = ctypes.c_void_p
                lib.NeAACDecInit2.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_char_p,
                    ctypes.c_ulong,
                    ctypes.POINTER(ctypes.c_ulong),
                    ctypes.POINTER(ctypes.c_ubyte),
                ]
                lib.NeAACDecInit2.restype = ctypes.c_byte
                lib.NeAACDecDecode.argtypes = [
                    ctypes.c_void_p,
                    ctypes.POINTER(_FaadFrameInfo),
                    ctypes.c_char_p,
                    ctypes.c_ulong,
                ]
                lib.NeAACDecDecode.restype = ctypes.c_void_p
                lib.NeAACDecClose.argtypes = [ctypes.c_void_p]
                lib.NeAACDecClose.restype = None
                _FAAD_LIB[0] = lib
                break
            except (OSError, AttributeError):
                continue
        return _FAAD_LIB[0]


# Decode the variable-length size in an MP4 descriptor.
def _read_mp4_descr_len(buf: bytes, pos: int) -> tuple[int, int]:
    length = 0
    for _ in range(4):
        if pos >= len(buf):
            return 0, pos
        b = buf[pos]
        pos += 1
        length = (length << 7) | (b & 0x7F)
        if not (b & 0x80):
            break
    return length, pos


# Read the AAC decoder configuration from a reference file or use defaults.
def _extract_aac_asc(ref_path: str) -> bytes:
    """Read AudioSpecificConfig from reference MP4/M4A `esds` box; fallback to 44.1kHz mono LC."""
    default_asc = b"\x12\x08"
    try:
        sz = os.path.getsize(ref_path)
        tail_sz = min(sz, 1024 * 1024)
        with open(ref_path, "rb") as f:
            f.seek(max(0, sz - tail_sz))
            tail = f.read(tail_sz)
        idx = tail.find(b"esds")
        if idx < 0:
            return default_asc
        pos = idx + 8  # skip 'esds' + version/flags
        for expected_tag, skip_bytes in ((0x03, 3), (0x04, 13), (0x05, 0)):
            if pos >= len(tail) or tail[pos] != expected_tag:
                return default_asc
            dlen, pos = _read_mp4_descr_len(tail, pos + 1)
            if expected_tag == 0x05 and dlen >= 2 and pos + dlen <= len(tail):
                return tail[pos : pos + dlen]
            pos += skip_bytes
    except OSError:
        pass
    return default_asc


# Recover playable AAC from raw frames in an interrupted recording.
def faad_recover_aac(fp: str, tmp_dir: str) -> tuple[str | None, int | None]:
    """Losslessly recover raw AAC frames from a moov-less MP4/3GP/M4A into tmp_dir via libfaad2."""
    lib = _get_libfaad()
    if not lib:
        return None, None
    refs = _find_reference_audio(fp, limit=1)
    asc = _extract_aac_asc(refs[0]) if refs else b"\x12\x08"
    val = int.from_bytes(asc[:2], "big")
    obj_type = (val >> 11) & 0x1F
    sr_idx = (val >> 7) & 0x0F
    ch_cfg = (val >> 3) & 0x0F
    if not (1 <= obj_type <= 4 and 0 <= sr_idx <= 12 and 1 <= ch_cfg <= 7):
        asc = b"\x12\x08"
        obj_type, sr_idx, ch_cfg = 2, 4, 1

    try:
        with open(fp, "rb") as f:
            raw = f.read()
    except OSError:
        return None, None

    idx = raw.find(b"mdat")
    if idx >= 4 and raw[idx - 4 : idx] == b"\x00\x00\x00\x01":
        start = idx + 12
    elif idx >= 0:
        start = idx + 4
    else:
        start = 0
    if start >= len(raw):
        return None, None

    data = memoryview(raw)[start:]
    n = len(data)
    dec = lib.NeAACDecOpen()
    if not dec:
        return None, None
    adts_frames = bytearray()
    good = 0
    try:
        sr = ctypes.c_ulong(0)
        ch = ctypes.c_ubyte(0)
        rc = lib.NeAACDecInit2(dec, ctypes.c_char_p(asc), len(asc), ctypes.byref(sr), ctypes.byref(ch))
        if rc < 0:
            return None, None
        info = _FaadFrameInfo()
        c_buf = ctypes.create_string_buffer(bytes(data), n)
        base_addr = ctypes.addressof(c_buf)
        off = 0
        profile = obj_type - 1
        while off + 4 < n:
            rem = min(8192, n - off)
            lib.NeAACDecDecode(dec, ctypes.byref(info), ctypes.c_char_p(base_addr + off), rem)
            # Decoder delay can leave the first valid frame without PCM output.
            # Preserve the frame and advance past it rather than scanning its interior.
            if info.error == 0 and 0 < info.bytesconsumed <= rem:
                flen = info.bytesconsumed
                full_len = 7 + flen
                adts_frames.extend((
                    0xFF,
                    0xF1,
                    (profile << 6) | (sr_idx << 2) | (ch_cfg >> 2),
                    ((ch_cfg & 3) << 6) | (full_len >> 11),
                    (full_len >> 3) & 0xFF,
                    ((full_len & 7) << 5) | 0x1F,
                    0xFC,
                ))
                adts_frames.extend(data[off : off + flen])
                off += flen
                good += 1
            else:
                off += 1
    finally:
        lib.NeAACDecClose(dec)

    if good < 5:
        return None, None
    adts_path = os.path.join(tmp_dir, "faad_recovered.aac")
    out_m4a = os.path.join(tmp_dir, "faad_recovered.m4a")
    with open(adts_path, "wb") as f:
        f.write(adts_frames)
    p = run_capture(
        ["ffmpeg", "-y", "-v", "error", "-i", adts_path, "-c", "copy", out_m4a],
        capture_output=True, text=True,
    )
    if p.returncode != 0 or not os.path.exists(out_m4a):
        return None, None
    ms, err = probe_duration_ms(out_m4a)
    if err or not ms or ms <= 0 or decode_audio_error(out_m4a):
        return None, None
    return out_m4a, ms


# Prepare verified audio for recognition, repairing and splitting it when needed.
def prepare_audio_work(
    cand: dict,
    tmp_dir: str,
    max_single_sec: int,
    chunk_sec: int,
    overlap_sec: int,
    untrunc_bin: str | None = None,
    use_faad: bool = True,
):
    """Probe, hash, recover/remux losslessly into tmp_dir, and slice if needed."""
    fp = cand["audio_abs"]
    if os.path.getsize(fp) == 0:
        return None, "empty_file"
    orig_sha = sha256_file(fp)
    work_src = fp
    dur_ms, err = probe_duration_ms(fp)
    with open(fp, "rb") as f:
        head = f.read(64)
    recoverable = err != "no_audio_stream" and (
        b"ftyp" in head or b"mdat" in head or os.path.splitext(fp)[1].lower() in {".m4a", ".mp4", ".3gp", ".tmp", ".crtemp"})
    if err and recoverable and use_faad:
        rec_path, rec_ms = faad_recover_aac(fp, tmp_dir)
        if rec_path and rec_ms:
            work_src = rec_path
            dur_ms = rec_ms
            err = None
    if err and recoverable and untrunc_bin:
        rec_path, rec_ms = untrunc_recover(fp, tmp_dir, untrunc_bin)
        if rec_path and rec_ms:
            work_src = rec_path
            dur_ms = rec_ms
            err = None
    if err:
        return None, err

    ext = os.path.splitext(work_src)[1].lower()
    remux_ext = ext if ext in {".m4a", ".aac", ".mp3", ".wav", ".ogg", ".opus", ".amr", ".flac"} else ".m4a"
    clean_path = os.path.join(tmp_dir, f"clean{remux_ext}")
    remux = run_capture(
        ["ffmpeg", "-y", "-v", "error", "-i", work_src,
         "-map", "0:a:0", "-vn", "-c:a", "copy", clean_path],
        capture_output=True, text=True,
    )
    if remux.returncode != 0 or not os.path.exists(clean_path) or os.path.getsize(clean_path) == 0:
        clean_path = os.path.join(tmp_dir, "decoded.wav")
        decoded = run_capture(
            ["ffmpeg", "-y", "-v", "error", "-xerror", "-i", work_src,
             "-map", "0:a:0", "-vn", "-c:a", "pcm_s16le", clean_path],
            capture_output=True, text=True,
        )
        if decoded.returncode != 0 or not os.path.exists(clean_path) or os.path.getsize(clean_path) == 0:
            return None, "ffmpeg audio preparation: " + (decoded.stderr.strip() or "empty output")[:300]

    if decode_error := decode_audio_error(clean_path):
        return None, decode_error

    parts = []
    if dur_ms > max_single_sec * 1000:
        step = chunk_sec - overlap_sec
        total_sec = dur_ms / 1000.0
        off = 0
        pnum = 1
        part_ext = os.path.splitext(clean_path)[1] or ".m4a"
        while off < total_sec - 1.0:
            ppath = os.path.join(tmp_dir, f"part_{pnum}{part_ext}")
            p = run_capture(
                ["ffmpeg", "-y", "-v", "error", "-ss", str(off), "-i", clean_path,
                 "-t", str(chunk_sec), "-c", "copy", ppath],
                capture_output=True, text=True,
            )
            if p.returncode != 0 or not os.path.exists(ppath):
                return None, f"ffmpeg slice error part {pnum}: {p.stderr.strip()}"
            pdur, perr = probe_duration_ms(ppath)
            if perr:
                return None, f"slice probe error part {pnum}: {perr}"
            parts.append({
                "part": pnum,
                "path": ppath,
                "offset_ms": off * 1000,
                "duration_ms": pdur,
                "overlap_ms": 0 if pnum == 1 else overlap_sec * 1000,
            })
            if off + chunk_sec >= total_sec:
                break
            off += step
            pnum += 1

    item = dict(cand)
    item["sha256"] = orig_sha
    item["duration_ms"] = dur_ms
    item["clean_abs"] = clean_path
    item["parts"] = parts or None
    return item, None

"""Offline checks for parsers, validation and quota reset handling."""

from datetime import datetime
from datetime import timezone
import json
import yaml

from .agy import extract_envelope_error_and_usage
from .quota import quota_wait_seconds
from .transcript import parse_speakers_and_lines, validate_and_render_md


# Check transcript parsing, validation, and quota handling without calling a model.
def run_selftest(args):
    people, orgs = {"ivanov_sergey": {"name": "Сергей Иванов"}}, {}
    sample_item = {
        "audio_rel": "Phone/2026/test.m4a",
        "sha256": "0" * 64,
        "duration_ms": 5000,
        "dt": "2026-11-15T12:00:00",
        "direction": "in",
        "contact": "Test",
        "phones": ["+70000000111"],
    }
    sp, lines, ns = parse_speakers_and_lines(
        'SPEAKERS_JSON: {"Сергей": {"person": "ivanov_sergey", "source": "speech", "note": "Голос владельца."}, '
        '"Оператор": {"person": null, "temp": true, "note": "Собеседник не представился."}}\n'
        "[00:00:01.00] **Сергей** Привет.\n"
        "[00:00:04.50] **Оператор:** Пока.\n"
        "[00:00:05.50] **?** [неразборчиво]\n"
    )
    md = validate_and_render_md(sample_item, sp, lines, ns, args.model, people, orgs)
    parsed_meta = yaml.safe_load(md.split("---", 2)[1])
    assert "[00:00:04.50] **Оператор** Пока." in md
    assert "[00:00:05.00] **?** [неразборчиво]" in md
    assert parsed_meta["schema_version"] == "1.0"
    assert parsed_meta["identification"] == "partial"
    assert "?" not in parsed_meta["speakers"]
    assert parsed_meta["speakers"]["Оператор"]["temp"] is True
    s_err, *_ = extract_envelope_error_and_usage(0, '{"status":"ERROR","response":"Your previous response was blocked by content safety filters"}', "")
    assert s_err and s_err.startswith("SAFETY:")
    quota_err, *_ = extract_envelope_error_and_usage(
        1, json.dumps({"status": "ERROR", "response": "Individual quota reached. "
                      "Please upgrade your subscription to increase your limits. Resets in 2h11m10s."}),
        "warning: --mode auto is not a",
    )
    assert quota_err and quota_err.startswith("QUOTA:")
    err_503, *_ = extract_envelope_error_and_usage(
        3, json.dumps({"status": "ERROR", "error": "Our servers are experiencing high traffic right now. (UNAVAILABLE (code 503): No capacity available)"}),
        'AGY_ERROR: {"status":"UNAVAILABLE","error_code":503}',
    )
    assert err_503 and err_503.startswith("ERR_503(")
    fixed_now = datetime(2026, 11, 15, 17, 9, 59, tzinfo=timezone.utc)
    for message, expected in [
        (quota_err, 7890),
        ("Resets in 45s.", 65),
        ("Resets in 10m.", 620),
        ("resets in 1h 5m", 3920),
        ("Resets in 1d2h", 93620),
        ("Resets in 0s", 30),
        ("Reset at 2026-11-15T19:21:09Z", 7890),
        ("Reset at 2026-11-15T22:21:09+03:00", 7890),
        ("Reset at 2026-11-16T01:09:59Z", 28820),
        ("Reset at 2026-11-15T17:00:00Z", 300),
        ("Resets in 2hours", 300),
        ("Resets in soon", 300),
        ("Quota exceeded", 300),
    ]:
        assert quota_wait_seconds(message, fixed_now) == expected, message
    print(
        f"SELFTEST_OK (models={','.join(args.model_chain)}, "
        f"faad={'ok' if args.use_faad else 'missing'}, untrunc={args.untrunc_bin or 'missing'})"
    )

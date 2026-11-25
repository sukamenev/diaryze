Read each audio file using read_file and return one complete result per ITEM.

$rules

Use EXACTLY this structure for each recording, without extra commentary:
=== ITEM <N> ===
SPEAKERS_JSON: {"Иван": {"person": null, "note": "Evidence or identification limitation"}}
SUMMARY_JSON: "Краткое содержание, не более 20 слов."
[00:00:00.50] **Иван** utterance
...
If no speech is audible, use NO_SPEECH instead of utterance lines.

$references

$items

The result failed validation: $error.
Correct and return only SPEAKERS_JSON, SUMMARY_JSON and utterance lines
[HH:MM:SS.cc] **<Speaker>** text (or **?** text), or NO_SPEECH when appropriate.
All times must be within 00:00:00.00 through $duration, preserving utterances and
speaker keys. SUMMARY_JSON must be a valid JSON string of at most 20 Russian words,
or null if a useful summary cannot be established.

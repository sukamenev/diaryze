Part $part_number failed transcript/timestamp validation: $error.
Correct the result without dropping utterances. Return SPEAKERS_JSON, SUMMARY_JSON,
and utterance lines [HH:MM:SS.cc] **<Speaker>** text (or **?** text), or NO_SPEECH if appropriate.
Use local times from 00:00:00.00 through $part_end; never add a global offset.
SUMMARY_JSON is a valid JSON string summarizing all parts heard so far, at most
20 words, or null if unavailable. Preserve existing evidence and speaker keys.

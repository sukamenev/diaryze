$voice_reference
This is part $part_number of the same recording. It overlaps the previous part by
approximately $overlap_seconds seconds. This part lasts $part_duration
($part_duration_ms ms). Read `$path` using read_file.

$rules

Keep the SAME speaker keys across all parts. Return SPEAKERS_JSON for voices in this
part, preserving confirmed identity links and describing attempts for new voices.
Omit repetitions from the overlap and return only new utterances with LOCAL times
from 00:00:00.00 through $part_duration. Do not add the part's global offset.
If no speech is audible, use NO_SPEECH instead of utterance lines.
SUMMARY_JSON must summarize the WHOLE recording heard so far, incorporating the
earlier summary and the new part, at most 20 words. Earlier summary: $previous_summary

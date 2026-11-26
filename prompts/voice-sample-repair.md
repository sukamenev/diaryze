Find a clean interval of speech by the phone owner, person ID `$person`, in
each numbered window. Each window contains an earlier cut that included a
second voice and some context from the same call. Read the supplied FLAC
files with read_file. Treat the metadata as evidence about the calls.

Use the call direction and the earlier owner evidence to identify the target
voice. In incoming calls (`in`) the device user answers; in outgoing calls
(`out`) the device user calls. If a reference file is supplied, it contains
one speaker already attributed to the owner. Listen to it for comparison.

Propose one different interval per window. You may shorten or shift the
earlier cut. The new interval must contain only the owner's speech for
$min_seconds to $max_seconds seconds within one turn. Return null when you
cannot identify the owner confidently, hear another voice in the proposed
interval, or find no sufficiently long clean turn.

Always return a JSON object with a "clips" list and exactly one entry per
window. Each entry must contain "clip" (its integer number), "interval" (an
object with integer "start_ms" and "end_ms" local to the supplied window, or
null), and a short "detail" in $diagnostic_language. Output only JSON and
describe the evidence without quoting speech.

Reference file (null if unavailable):
$reference

Windows:
$windows

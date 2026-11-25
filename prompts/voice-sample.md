Find up to three short reference clips of the usual owner of the phone, person ID `$person`.
Each numbered input is a separate call from the same phone with a different contact.
Attempt to read every supplied file with read_file. In incoming calls (`in`) the device user
answers; in outgoing calls (`out`) the device user calls. Compare audible voices
across calls.
Require the same audible voice in at least two calls, with language or call roles
that support identifying it as the usual device user. Consider borrowed phones,
background speakers, bots, voicemail, and common call greetings before deciding.
Choose $min_seconds to $max_seconds seconds per clip of clear, continuous natural
speech from one call with no overlapping voices. Prefer clips of at least 10
seconds when they have comparable clarity and identity evidence. Shorter clean
clips within the allowed range are valid candidates. Prefer different calls or
non-overlapping intervals of the same call. List your strongest candidates first.
Always return a JSON object with all of these fields, including when no clip qualifies:

- "summary": A short explanation of the outcome, including the main reason when
  no clip qualifies. Write this and each call's "detail" in $diagnostic_language.
- "calls": Exactly one entry for each supplied call. Each entry must have
  "call" (its integer number), "read_status" ("read" if you heard the audio,
  "unreadable" if the tool did not provide usable audio, or "not_attempted" if
  no read was attempted), and "detail" (what the audio showed about the match or
  clip quality, or the reason reading failed or was not attempted). Keep each
  detail brief. Report observed tool errors if available. Seeing a path,
  metadata, or text alone does not count as hearing audio.
- "candidates": Up to three objects such as
  {"call":1,"start_ms":12000,"end_ms":25000,"supporting_calls":[2],"evidence":"Why these calls support the owner match"}.
  Use integer millisecond offsets local to each chosen recording. Only use calls
  with read_status "read" in a candidate or as supporting evidence. Use [] when
  no candidate qualifies, and still fill in "summary" and "calls".

Output only JSON. Describe the evidence without quoting the conversation.

Calls:
$calls

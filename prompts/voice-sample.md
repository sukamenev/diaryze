Find up to $max_candidates short reference clips of the usual owner of the phone, person ID `$person`.
Each numbered input is a separate call from the same phone with a different contact.
Attempt to read every supplied file with read_file. In incoming calls (`in`) the device user
answers; in outgoing calls (`out`) the device user calls. Compare audible voices
across calls.
Require the same audible voice in at least $min_calls calls, with language or call roles
that support identifying it as the usual device user. Consider borrowed phones,
background speakers, bots, voicemail, and common call greetings before deciding.
Locate a continuous turn spoken by the owner and choose an interval entirely
within that turn. Each clip must contain $min_seconds to $max_seconds seconds
of clear natural speech from the owner alone. Reject intervals with any other
voice, whether it overlaps the owner's speech or takes a separate turn. Prefer
clips of at least $preferred_seconds seconds when clarity and identity evidence are comparable.
Shorter clean clips within the allowed range are valid. Prefer different calls
or non-overlapping intervals of the same call. List your strongest candidates first.
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
- "candidates": Up to $max_candidates objects. Each must have integer "call",
  "start_ms", and "end_ms", a "supporting_calls" list of other integer call
  numbers, and brief "evidence" explaining the owner match. Offsets are local to
  the chosen recording. Only use calls marked "read" in a candidate or as
  supporting evidence. Use [] when no candidate qualifies, and still fill in
  "summary" and "calls".

Output only JSON. Describe the evidence without quoting the conversation.

Calls:
$calls

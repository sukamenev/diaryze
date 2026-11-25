Find up to three short reference clips of the usual owner of the phone, person ID `$person`.
Each numbered input is a separate call from the same phone with a different contact.
Read the supplied files with read_file. In incoming calls (`in`) the device user
answers; in outgoing calls (`out`) the device user calls. Compare audible voices
across calls.
Require the same audible voice in at least two calls, with language or call roles
that support identifying it as the usual device user. Consider borrowed phones,
background speakers, bots, voicemail, and common call greetings before deciding.
Choose 10 to 20 seconds per clip of clear, continuous natural speech from one
call with no overlapping voices. Prefer different calls or non-overlapping
intervals of the same call. List your strongest candidates first.
Return a JSON array with up to three objects, with integer call numbers and
millisecond offsets local to each chosen input, for example:
[{"call":1,"start_ms":12000,"end_ms":25000,"supporting_calls":[2],"evidence":"Brief reason the same voice is the owner across the calls"}]
If the owner's voice is uncertain or no suitable interval is available, return
an empty array. Output only JSON.

Calls:
$calls

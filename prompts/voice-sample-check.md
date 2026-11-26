Check whether each numbered audio clip contains speech from exactly one person.
Read every supplied FLAC with read_file. Judge the clips individually from their
audio. A clip qualifies only if the same speaker is the sole audible voice
throughout. Any speech from another person disqualifies it, whether the voices
overlap or speak in turn. Count audible background speech as another voice.
Ignore silence and non-speech sounds. You do not need to identify the speaker.

Always return a JSON object with a "clips" list and exactly one entry per input.
Each entry must have "clip" (its integer number), "status", and a short "detail"
in $diagnostic_language. Use one of these status values:

- "single_speaker" if you heard the entire clip and are confident exactly one person spoke.
- "multiple_speakers" if you heard speech from more than one person.
- "no_speech" if you heard the clip and there is no speech.
- "uncertain" if you cannot confidently count the voices.
- "unreadable" if you could not listen to the audio.

Output only JSON. Describe the evidence without quoting speech.

Clips:
$clips

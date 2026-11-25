Read the audio file `$path` using read_file. It is a collection of isolated
excerpts of natural Russian telephone speech separated by silence.
Transcribe every excerpt. Start a new line at the onset of each excerpt,
even if the same voice continues; include audible interjections.
Use S1, S2, etc. consistently for audible voices.

$timestamp_rule

Return only timestamped transcript lines: [time] **S1:** text.
Listen for actual speech onsets. Do not infer times from the text or assumed
speaking speed. Do not use any tools other than read_file. Read only the
specified audio, never reference files or other benchmark artifacts.

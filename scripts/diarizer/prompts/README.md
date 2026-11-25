# Prompt templates

English instructions live here; Python only renders recording and catalog data.

- `rules.md`: shared requirements for every processing mode, identity evidence,
  summaries, and honest timestamp precision.
- `batch.md`: one or several complete recordings in a request.
- `part-first.md`, `part-next.md`: long recordings; the final cumulative summary
  describes all parts heard so far.
- `part-context.md`: restores catalog, ownership and known speaker context when
  a later part starts in a new model session after fallback.
- `repair-single.md`, `repair-part.md`: response correction without a new audio task.
- `owner-*.md`: folder ownership hints (used unless speech suggests someone else is using the phone).
- `audio-reader.md`: minimal agy agent profile.
- `voice-reference.md`, `voice-sample.md`: use approved voice references and find review candidates.
- `timing-benchmark.md`, `timing-duration.md`: prompts for timestamp benchmark runs.

Responses use SPEAKERS_JSON, SUMMARY_JSON, and timestamped utterances or NO_SPEECH.
SUMMARY_JSON is a JSON string of at most 20 Russian words, or null. Spam scoring is
not requested. Python writes the same schema 1.0 fields for batches and long audio.
Changes to these files affect subsequent template loads; running Python processes
must be restarted to pick up changes to their rendering code.

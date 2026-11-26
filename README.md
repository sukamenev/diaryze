# diaryze

English · [Русский](README.ru.md)

Turn phone calls and voice recordings into searchable Markdown, one file per recording. Transcripts include timestamped speech in the original language, speaker names or roles, links to people or organizations, and a short summary in Russian. [Format 1.0](docs/formats.md)

## Install and run

Requires Linux or macOS, Python 3.11+, and [FFmpeg](https://ffmpeg.org/download.html) (`ffmpeg` and `ffprobe` on `PATH`). On macOS, run `brew install ffmpeg`. Install [Google Antigravity CLI](https://antigravity.google/docs/getting-started) and run `agy` to sign in to an account with access to your models. PyYAML is installed with the package. [Optional recovery tools](#audio-recovery)

```bash
cd "/path/to/recordings"
git clone https://github.com/sukamenev/diaryze.git
python3 -m venv diaryze/.venv
source diaryze/.venv/bin/activate
python -m pip install ./diaryze
diaryze --dry-run
```

Git names the checkout `diaryze` by default. You can pass a different destination name to `git clone` and use it in the paths above. Keep `00_Diaryze_Data/` for archive data. Diaryze skips the checkout when it looks for recordings.

From the archive root, try processing a few recordings.

```bash
diaryze --workers 2 --limit 5
```

diaryze uses the current directory as the archive root by default. Use `diaryze --root "/path/to/recordings"` from elsewhere. Use `--folder Phone/2025` to limit both transcription and candidate preparation to a folder within the archive. With PyYAML already installed, `python3 /path/to/diaryze/scripts/diarize.py` accepts the same options.

The command-line interface is in English by default. Use `--ui ru` for Russian help, progress and prompts, or `--ui en` to select English explicitly. Transcript language and summary rules are independent of the interface.

If you have a checkout elsewhere, you can put a symlink named `diaryze` in the archive. With PyYAML installed, the direct script command works from the archive root:

```bash
python3 diaryze/scripts/diarize.py --folder Phone/2025 --dry-run
```

`--models model-a,model-b` sets the full model chain. Pass one name to use one model. `--model` sets the first model and keeps the configured fallbacks. diaryze batches short recordings, splits long ones with overlap, and maps timestamps back to the original. Reruns skip transcripts with a matching source hash. An interrupted long recording resumes from saved parts: the checkpoint is tied to the audio hash and part layout and is removed after the transcript is published.

In the author's runs, **5 hours of Antigravity quota covered roughly 18–26 hours of recorded conversations**. Throughput varies with models, recordings, retries, and worker count.

## What the archive contains

- `00_Diaryze_Data/Text/` mirrors the source paths. `Phone/2025/call.m4a` becomes `00_Diaryze_Data/Text/Phone/2025/call.m4a.md`.
- `00_Diaryze_Data/people.yaml` and `00_Diaryze_Data/orgs.yaml` hold the catalogs. `00_Diaryze_Data/manifest.jsonl` records processing history.
- `00_Diaryze_Data/voice_samples/<person>/<phone|recorder>/confirmed/YYYY-MM-DD_NN.flac` holds approved voice references, independently of the source calls.
- `00_Diaryze_Data/voice_samples/<person>/<phone|recorder>/candidate/` holds clips awaiting review.
- `00_Diaryze_Data/logs/` holds diagnostics, which can contain model responses.
- `00_Diaryze_Data/.work/checkpoints/` holds accepted long-recording parts until the final transcript is published.
- `00_Diaryze_Data/reports/` holds archive-specific reports and benchmark results.

An archive with the older `Data/` directory can continue using it. Catalogs and manifests in legacy `docs/` are also supported. Originals stay in place. `agy` sends the audio and transcription context to the model provider.

## Identify speakers

Processing creates missing catalogs. To fill them first, run `diaryze --root "/path/to/recordings" --init` and edit the YAML. Existing catalogs are preserved.

Example card in `00_Diaryze_Data/people.yaml`.

```yaml
schema_version: "1.0"
people:
  ivanov_ivan:
    name: Иван Иванов
    aliases: [Ваня]
    tags: [owner]
```

Place a `.owner` file in a call folder and set `owner` to an ID from `people.yaml`. The nearest `.owner` applies to nested folders.

```yaml
type: phone
owner: ivanov_ivan
label: Иван
```

diaryze links speakers using filenames, phone numbers, catalog matches, and speech. With enough evidence, it adds a card after checking known names and numbers. Review automatic links and keep card IDs stable because transcripts refer to them. Unknown speakers have `person: null`. One-off participants can use `temp: true` without a card.

### Phone owner voice references

On the first run for phone folders without a known owner's sample, diaryze compares short calls with different contacts inside the selected folder and cuts up to three candidate clips per `.owner` folder. It can find `.owner` in a parent directory up to the archive root. It stops before transcription and prints the files under `00_Diaryze_Data/voice_samples/<person>/phone/candidate/` for listening.

Candidates can contain 5–20 seconds of clean speech from one speaker. Clips of at least 10 seconds are preferred when quality and identity evidence are comparable.
A separate model request checks each extracted FLAC. If a second voice is heard, diaryze makes one attempt to shorten or shift the cut using up to five seconds of source audio on either side. It checks the new cut again. Only clips judged to contain one speaker reach the candidate folder.

Listen to the clips in `candidate/` and move the best FLAC to the adjacent `confirmed/` folder. Leave its JSON sidecar in `candidate/`. A rerun before approval shows the existing candidates. Once you move a clip, diaryze verifies it, saves its provenance, and continues with transcription. For each call, it uses one reference from the closest date in either direction. A phone clip wins ties. If the call date is unavailable, the most recent dated reference is used.

`--prepare-voice-sample` prepares candidates for the selected folder’s owner without starting a transcription. Add a person ID to select one explicitly. `--approve-voice-sample "path/to/2025-03-14_01.flac"` confirms a clip, and `--reject-voice-sample "path/to/2025-03-14_01.flac"` discards its set.

The review prints instructions when clips are ready. If it cannot prepare any, it prints a short error and the log path. The log in `00_Diaryze_Data/logs/` contains the input paths, per-call reading status and the full model replies for selection, verification and any corrections.

`--no-voice-samples` starts transcription without preparing or using samples. `--skip-voice-sample-preflight` starts transcription immediately and uses any approved references available.

## Audio recovery

diaryze works on temporary copies. It uses FFmpeg to copy the first readable audio stream or decode it to WAV if copying fails. For a damaged AAC container such as an M4A missing `moov`, it tries [FAAD2](https://github.com/knik0/faad2) first, then remuxes surviving frames with FFmpeg. Install the shared library with `brew install faad2` on macOS or `sudo apt install libfaad2` on [Debian](https://packages.debian.org/trixie/libfaad2).

If AAC recovery fails, diaryze can use `untrunc` on `PATH` with `-dst` support and a playable M4A or MP4 in the same folder. A reference from the same device and settings works best. Manual Docker use and build instructions are available upstream. [untrunc](https://github.com/anthwlock/untrunc#building)

Recovered audio and the prepared stream must pass a full strict decode before transcription. If the provider still rejects media as empty, corrupted, or malformed, diaryze records `needs_recovery` instead of sending the same bytes to every model. A rejected batch is retried one file at a time so healthy recordings can finish.

If a file remains unreadable, diaryze skips transcription and records `needs_recovery` with the reason and source hash. Empty files and files without audio get `skipped`. After installing recovery tools, run `diaryze --retry-recovery` to include flagged files and damaged files skipped by earlier versions. Files tagged `needs_recovery` reenter the regular queue if their bytes change. Use `--no-faad` or `--no-untrunc` to disable a method.

## Search and maintain an archive

Run the utility scripts from the archive using their paths in the checkout. [find.py](scripts/find.py) searches speech and summaries, filters by person, organization, phone, date, or duration, and outputs a table, Markdown or audio paths, or JSONL. It also accepts `--root`.

```bash
python3 /path/to/diaryze/scripts/find.py --text 'contract|договор' --limit 20
python3 /path/to/diaryze/scripts/find.py --person ivanov_ivan --from 2025-01-01
python3 /path/to/diaryze/scripts/find.py --root "/path/to/recordings" --folder Phone/2025 --direction in --format paths
```

| Script | Purpose |
| --- | --- |
| [find_audio_duplicates.py](scripts/find_audio_duplicates.py) | Report byte-identical audio files. |
| [audit_recording_dates.py](scripts/audit_recording_dates.py) | Compare dates from filenames, metadata, and file times. Changing audio timestamps requires a plan and `--apply`. |
| [sync_md_dates.py](scripts/sync_md_dates.py) | Check transcript file times against recording dates. Use `--apply` to sync. |
| [swap_speakers.py](scripts/swap_speakers.py) | Swap two speaker labels in the timestamped turns of one transcript. Run with `"00_Diaryze_Data/Text/recording.m4a.md" "Alice" "Bob"` to preview. Add `--apply` to save. |
| [relink_catalog_speakers.py](scripts/relink_catalog_speakers.py) | Match unlinked speakers to existing cards. |
| [migrate_identification.py](scripts/migrate_identification.py), [migrate_phone_owners.py](scripts/migrate_phone_owners.py), [migrate_catalog_fields.py](scripts/migrate_catalog_fields.py) | Preview transcript migrations. Use `--apply` to write changes. |

Model and worker defaults, timeouts, and voice sample limits are in [config.py](scripts/diarizer/config.py). Model instructions are in [prompts/](prompts/README.md). Put temporary scripts in the Git-ignored `scripts/local/` directory of the checkout.

From the checkout, `diaryze --selftest` checks tool availability. Run `python3 -m unittest discover -s scripts/tests -q` for offline tests with synthetic audio and mock model responses.

[MIT license](LICENSE)

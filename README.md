# diaryze

English · [Русский](README.ru.md)

Transcribe phone calls and voice recordings, identify who spoke when, and save one searchable Markdown file per recording. Each transcript contains timestamped utterances with speaker names or roles, a short summary, and links to people or organizations. See the [format 1.0 specification (Russian)](docs/formats.md).

Keep the code in its own checkout. Each audio archive holds its own transcripts, catalogs, and processing history, so they can move with the recordings.

## Installation

- Linux with Python 3.11 or later.
- `ffmpeg` and `ffprobe` from [FFmpeg](https://ffmpeg.org/download.html) on `PATH`.
- [Google Antigravity CLI](https://antigravity.google/docs/getting-started) (`agy`). Run `agy` to sign in and make sure your account can use the models you select.
- PyYAML is required to read and write transcript metadata and catalogs. The installation below installs it into a virtual environment.
- `libfaad2` and `untrunc` are optional [audio recovery tools](#audio-recovery).

```bash
git clone https://github.com/sukamenev/diaryze.git
cd diaryze
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
```

## Usage

With the environment activated, run `diaryze` from the folder containing your recordings. Use `--dry-run` to inspect the queue, then try a few files.

```bash
cd "/path/to/recordings"
diaryze --dry-run
diaryze --workers 2 --limit 5
```

To run from another directory, pass the archive path.

```bash
diaryze --folder "/path/to/recordings" --workers 2
```

To process a single subfolder while keeping the output in the shared archive root, pass both options.

```bash
diaryze --root "/path/to/recordings" --folder Phone/2025
```

If PyYAML is already available to your Python, you can also run `python3 /path/to/diaryze/scripts/diarize.py` with the same arguments.

`--models model-a,model-b` sets the full model chain. Use `--models model-a` for a single model. `--model` sets the first model and adds the configured fallbacks. Short recordings can share a request. Long recordings are split into overlapping parts, and their timestamps are mapped back to the original recording. Reruns skip completed transcripts whose source audio hash matches.

In the author's runs, **five hours of Antigravity quota covered roughly 18–26 hours of recorded conversations**. This varies with model choice, the recordings, retries, and the number of workers.

`agy` sends the audio being processed and its transcription context to the model provider. Transcripts, catalogs, and logs are stored in the local archive. Diagnostic logs can contain model responses.

## Audio recovery

| Condition | What diaryze does |
| --- | --- |
| FFprobe can read the audio stream and its duration | FFmpeg copies the first audio stream into a clean container. If stream copy fails, diaryze tries to decode the stream into WAV. |
| FFprobe cannot open an unfinished container, for example `moov atom not found` | For eligible files, diaryze first tries to recover AAC frames with [FAAD2](https://github.com/knik0/faad2). FFmpeg builds a new container from the recovered frames. |
| AAC recovery fails | If `untrunc` is on `PATH`, diaryze tries a playable M4A or MP4 in the same folder as a reference. A sample from the same device with the same settings is a better match. |

**Use `libfaad2` for AAC recovery without `untrunc`.** On [Debian](https://packages.debian.org/trixie/libfaad2), install it with `sudo apt install libfaad2`. diaryze loads `libfaad.so.2` directly, so the separate `faad` executable is unnecessary. Recovery depends on how much of the AAC data survived. FFmpeg stream copy alone may be insufficient when the container index is missing.

If your distribution has no `untrunc` package, [anthwlock/untrunc](https://github.com/anthwlock/untrunc#building) has build instructions and a Docker option for manual recovery. For automatic recovery, diaryze needs an `untrunc` executable on `PATH` that supports `-dst`.

Recovery works on temporary files and preserves the original recording. Run `--selftest` to see which recovery tools are available. Use `--no-faad` or `--no-untrunc` to disable a recovery method.

## Archive layout

```text
recordings/
├── Phone/2025/call.m4a
├── Recordings/memo.wav
├── Text/
│   ├── Phone/2025/call.m4a.md
│   └── Recordings/memo.wav.md
├── docs/
│   ├── people.yaml
│   ├── orgs.yaml
│   └── manifest.jsonl
└── work/logs/
```

The output mirrors each source path under `Text/`, keeping the audio extension. `manifest.jsonl` records processing history, and `work/logs/` contains diagnostic logs.

## People and organizations

The first processing run creates empty `docs/people.yaml` and `docs/orgs.yaml` if needed. To fill them in before processing, run `diaryze --folder "/path/to/recordings" --init` and edit the YAML. The command preserves existing catalogs. Here is a fictional example.

```yaml
schema_version: "1.0"
people:
  ivanov_ivan:
    name: Иван Иванов
    aliases: [Ваня]
    tags: [owner]
```

Create a `.owner` file in a folder of calls to identify the owner of the recording phone.

```yaml
type: phone
owner: ivanov_ivan
label: Иван
```

`owner` refers to an ID in `people.yaml`. The nearest `.owner` applies to nested folders, so different phones can have different owners. Unidentified speakers retain `person: null`.

As diaryze processes recordings, it uses filenames, phone numbers, the existing catalogs, and evidence from speech to suggest speaker links. It can add people or organizations when there is enough evidence and checks new entries against known names and numbers. A one-off participant can be marked `temp: true` without a catalog entry. You can review and correct automatic links. Keep IDs stable because transcripts refer to them.

## Search and other scripts

Run the scripts from the archive using their paths in the checkout. `find.py` also accepts `--root`.

```bash
python3 /path/to/diaryze/scripts/find.py --text 'contract|договор' --limit 20
python3 /path/to/diaryze/scripts/find.py --person ivanov_ivan --from 2025-01-01
python3 /path/to/diaryze/scripts/find.py --root "/path/to/recordings" --folder Phone/2025 --direction in --format paths
```

| Script | Purpose |
| --- | --- |
| [diarize.py](scripts/diarize.py) | Main processor and source checkout equivalent of `diaryze`. |
| [find.py](scripts/find.py) | Search speech and summaries, filter by people, organizations, phone numbers, dates, or duration, and output a table, Markdown paths, audio paths, or JSONL. |
| [find_audio_duplicates.py](scripts/find_audio_duplicates.py) | Find byte-for-byte duplicate audio and write a report. |
| [audit_recording_dates.py](scripts/audit_recording_dates.py) | Compare dates from filenames, metadata, and audio modification times. Applying changes requires a prepared plan and `--apply`. |
| [sync_md_dates.py](scripts/sync_md_dates.py) | Compare Markdown modification times with recording dates. Use `--apply` to sync them. |
| [relink_catalog_speakers.py](scripts/relink_catalog_speakers.py) | Check whether unidentified voices in existing transcripts can be linked to catalog entries. |
| [migrate_identification.py](scripts/migrate_identification.py), [migrate_phone_owners.py](scripts/migrate_phone_owners.py), [migrate_catalog_fields.py](scripts/migrate_catalog_fields.py) | Preview updates to existing transcripts. Use `--apply` to write the updates. |

Defaults and model fallback policies are in [config.py](scripts/diarizer/config.py). Model instructions are in [promts/](scripts/diarizer/promts/README.md). Speech stays in the original language. Short summaries are written in Russian.

From the checkout with the package installed, run the offline checks.

```bash
diaryze --selftest
python3 -m unittest discover -s scripts/tests -q
```

The tests use synthetic audio and mock model responses.

[MIT license](LICENSE)

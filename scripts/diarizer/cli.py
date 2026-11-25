"""Command-line options, signals and the worker pool."""

from collections import deque
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait
import argparse
import os
from pathlib import Path
import shutil
import threading
import yaml

from .archive import init_catalogs, load_refs, resolve_source_dir, scan_pending
from .audio import _get_libfaad, find_untrunc
from .config import (
    BATCH_SHORT_MAX, BATCH_SHORT_SEC, CHUNK_SEC, DEFAULT_FALLBACK_MODELS,
    DEFAULT_MODEL, DEFAULT_WORKERS, MAX_FILE_ATTEMPTS, MAX_SINGLE_SEC,
    OVERLAP_SEC, REQUEST_TIMEOUT_SEC, use_archive,
)
from .pipeline import Stats, pack_next_job, run_job
from .runtime import STOP_EVENT, stop_signals
from .console import emit, progress_output, render_finished
from .selftest import run_selftest
from .terminal import preserve_terminal
from .diagnostics import record, run_log
from .errors import ModelsUnavailable


# Parse CLI options, select the archive, and initialize processing.
def main():
    ap = argparse.ArgumentParser(description="Parallel agy audio diarizer")
    ap.add_argument("--root", help="Archive root, useful with --folder to limit the scan")
    ap.add_argument("--folder", "--subdir", dest="subdir", default=".", metavar="PATH",
                    help="Archive root (default: current directory), or subfolder when --root is set")
    ap.add_argument("--init", action="store_true", help="Create empty local catalogs and exit")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--models", default="", help="Comma-separated ordered model list (overrides --model + --fallback-models)")
    ap.add_argument("--fallback-models", default=DEFAULT_FALLBACK_MODELS,
                        help="Comma-separated fallback models for provider and task errors")
    ap.add_argument("--fallback-on-quota", action="store_true",
                    help="Compatibility option: quota fallback is now automatic")
    ap.add_argument("--limit", type=int, default=0, help="Max pending files to process (0 = all)")
    ap.add_argument("--batch-short-max", type=int, default=BATCH_SHORT_MAX, help="Max short files per agy call")
    ap.add_argument("--batch-short-sec", type=int, default=BATCH_SHORT_SEC, help="Target max short audio sec per agy call")
    ap.add_argument("--max-single-sec", type=int, default=MAX_SINGLE_SEC, help="Slice recordings longer than this")
    ap.add_argument("--chunk-sec", type=int, default=CHUNK_SEC)
    ap.add_argument("--overlap-sec", type=int, default=OVERLAP_SEC)
    ap.add_argument("--timeout", type=int, default=REQUEST_TIMEOUT_SEC)
    ap.add_argument("--no-custom-agent", action="store_true", help="Disable minimal custom agent")
    ap.add_argument("--no-untrunc", action="store_true", help="Disable automatic untrunc fallback for damaged containers")
    ap.add_argument("--no-faad", action="store_true", help="Disable libfaad2 raw AAC frame recovery")
    ap.add_argument("--retry-skipped", action="store_true", help="Re-try files previously marked status=skipped in manifest.jsonl")
    ap.add_argument("--retry-recovery", action="store_true",
                    help="Retry files marked needs_recovery and legacy damaged files marked skipped")
    ap.add_argument("--retry-refused", action="store_true", help="Re-try files previously marked status=refused in manifest.jsonl")
    ap.add_argument("--stop-on-quota", action="store_true", help="Exit on quota exhaustion instead of sleeping")
    ap.add_argument("--max-file-attempts", type=int, default=MAX_FILE_ATTEMPTS,
                    help="Max queue assignments per unchanged file in one run (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="List pending queue without running agy")
    ap.add_argument("--selftest", action="store_true", help="Run internal parser/validator/recovery self-test")
    args = ap.parse_args()
    if args.max_file_attempts < 1:
        ap.error('--max-file-attempts must be positive')
    raw_chain = args.models if args.models.strip() else ",".join(
        x for x in (args.model, args.fallback_models) if x.strip()
    )
    args.model_chain = []
    for m in raw_chain.split(","):
        ms = m.strip()
        if ms and ms not in args.model_chain:
            args.model_chain.append(ms)
    if not args.model_chain:
        args.model_chain = [args.model]
    args.model = args.model_chain[0]
    args.untrunc_bin = None if args.no_untrunc else find_untrunc()
    args.use_faad = False if args.no_faad else (_get_libfaad() is not None)

    if args.selftest:
        run_selftest(args)
        return

    root = args.root if args.root is not None else args.subdir
    if args.root is None:
        args.subdir = "."
    with use_archive(root) as archive_root:
        args.root = archive_root
        try:
            resolve_source_dir(args.subdir)
        except ValueError as exc:
            ap.error(str(exc))
        if args.init:
            init_catalogs()
            emit(f"Справочники: {Path(archive_root) / 'docs'}")
            return
        if not args.dry_run:
            missing = [tool for tool in ("agy", "ffmpeg", "ffprobe") if not shutil.which(tool)]
            if missing:
                ap.error("Missing executable(s) on PATH: " + ", ".join(missing))
            try:
                load_refs()
            except (OSError, ValueError, yaml.YAMLError) as exc:
                ap.error(str(exc))
            init_catalogs()
            (Path(archive_root) / "Text").mkdir(exist_ok=True)
        return _run(args)


# Run the worker pool with signal handling, terminal cleanup, and diagnostics.
def _run(args):
    with stop_signals(), preserve_terminal(), progress_output():
        if args.dry_run:
            return run_workers(args)
        with run_log() as path:
            emit(f'Журнал диагностики: {path}', flush=True)
            record('run_started', options=vars(args))
            try:
                result = run_workers(args)
                return 1 if result and result['failed_files'] else 0
            except ModelsUnavailable as exc:
                record('fatal_error', error=exc)
                emit(f'[ERR] {exc}')
                return 1
            except BaseException as exc:
                record('fatal_error', error=exc)
                raise


# Scan the pending queue, coordinate workers, and report final totals.
def run_workers(args):
    cands = scan_pending(args.subdir, retry_skipped=args.retry_skipped, retry_refused=args.retry_refused,
                         retry_recovery=getattr(args, "retry_recovery", False))
    if args.limit > 0:
        cands = cands[: args.limit]
    faad_state = "ok" if args.use_faad else ("disabled" if args.no_faad else "missing")
    untrunc_state = args.untrunc_bin or ("disabled" if args.no_untrunc else "missing")
    models_str = " → ".join(args.model_chain)
    emit(f"Очередь {args.subdir}: {len(cands)} файлов · faad {faad_state} · untrunc {untrunc_state}", flush=True)
    if not args.use_faad and not args.no_faad:
        emit("[WARN] libfaad2 не найдена — восстановление сырых AAC кадров отключено", flush=True)
    if not args.untrunc_bin and not args.no_untrunc:
        emit("[WARN] untrunc не найден в PATH — запасная автопочинка контейнеров отключена", flush=True)
    if args.dry_run:
        if len(cands) > 20:
            emit(f"Показаны первые 20 из {len(cands)}:")
        for i, c in enumerate(cands[:20], 1):
            emit(f"  {i:2d}. {c['dt'] or '?'} | {c['audio_rel']}")
        return
    if not cands:
        record('run_finished', summary={'done': 0, 'failed_files': 0}, failed_files=[])
        return

    pending_dq = deque(cands)
    active_rels = set()
    attempts = Counter()
    max_attempts = getattr(args, 'max_file_attempts', MAX_FILE_ATTEMPTS)
    stats = Stats(pending_count=lambda: len(pending_dq))
    emit(f"Старт · работников {args.workers} · модели {models_str}", flush=True)

    q_lock = threading.Lock()

    # Track retry attempts against a particular version of a source file.
    def file_key(item):
        return item['audio_rel'], item.get('mtime'), os.path.getsize(item['audio_abs'])

    # Claim each job under the queue lock and release its files when finished.
    def work():
        while not STOP_EVENT.is_set():
            with q_lock:
                if not pending_dq and args.limit == 0:
                    fresh = [
                        c for c in scan_pending(
                            args.subdir, retry_skipped=args.retry_skipped, retry_refused=args.retry_refused,
                        )
                        if c["audio_rel"] not in active_rels and attempts[file_key(c)] < max_attempts
                    ]
                    pending_dq.extend(fresh)
                job = pack_next_job(pending_dq, args.batch_short_max, args.batch_short_sec, args.max_single_sec)
                if job:
                    for it in job:
                        active_rels.add(it["audio_rel"])
                        attempts[file_key(it)] += 1
                    record('job_started', items=job,
                           attempts={it['audio_rel']: attempts[file_key(it)] for it in job})
            if not job:
                return
            try:
                run_job(job, args, stats)
            finally:
                with q_lock:
                    for it in job:
                        active_rels.discard(it["audio_rel"])

    # Handle cancellation and stop the pool if a worker fails unexpectedly.
    def worker_loop():
        try:
            work()
        except InterruptedError:
            return
        except Exception as exc:
            STOP_EVENT.set()
            record('worker_error', error=exc, stage='worker_loop')
            raise

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futs = [pool.submit(worker_loop) for _ in range(args.workers)]
            wait(futs)
            for future in futs:
                future.result()
    finally:
        summary = stats.snapshot()
        record('run_finished', summary=summary, failed_files=sorted(stats.failed_files))
        emit(render_finished(summary, stopped=STOP_EVENT.is_set()), final=True)
    return summary

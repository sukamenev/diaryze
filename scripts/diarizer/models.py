"""Shared model availability and task-local fallback, driven by configured policies."""

from datetime import datetime, timedelta
import math
import threading
import time

from .config import MODEL_ERROR_POLICIES, MODEL_WAIT_POLL_S, TZ
from .console import _display, emit
from .diagnostics import record
from .errors import LABELS, ModelRefused, ModelsUnavailable, ModelTaskFailed, classify_error
from .quota import quota_wait_seconds
from .runtime import STOP_EVENT


class ModelCooldowns:
    def __init__(self):
        self._until = {}
        self._disabled = {}
        self._streak = {}
        self._lock = threading.Lock()

    def _expire_locked(self, now):
        """Start a fresh error streak after a cooldown; caller holds _lock."""
        for model, until in list(self._until.items()):
            if until <= now:
                self._until.pop(model)
                self._streak.pop(model, None)

    def remaining(self, model):
        with self._lock:
            now = time.monotonic()
            self._expire_locked(now)
            if model in self._disabled:
                return math.inf
            return max(0, self._until.get(model, 0) - now)

    def success(self, model):
        # An in-flight success must not cancel a pause recorded by another worker.
        with self._lock:
            self._streak.pop(model, None)

    def block(self, model, *, kind="503", seconds=None, items=(), scope=""):
        seconds = MODEL_ERROR_POLICIES[kind]['cooldown_s'] if seconds is None else seconds
        with self._lock:
            now = time.monotonic()
            self._expire_locked(now)
            until = max(self._until.get(model, 0), now + seconds)
            self._until[model] = until
            seconds = max(0, until - now)
        resume_at = datetime.now(TZ) + timedelta(seconds=seconds)
        record('model_cooldown', items=items, model=model, reason=kind, policy_scope='model',
               seconds=seconds, resume_at=resume_at.isoformat(timespec='seconds'))
        prefix = f"{scope}: " if scope else ""
        emit(f"[WARN] {prefix}{LABELS[kind]} на {model}; пауза {math.ceil(seconds)} с "
             f"для всех работников (до {resume_at:%H:%M:%S %z})")

    def failed(self, model, error, *, items=(), scope=""):
        kind = classify_error(error)
        policy = MODEL_ERROR_POLICIES[kind]
        with self._lock:
            self._expire_locked(time.monotonic())
            previous, count = self._streak.get(model, (None, 0))
            count = count + 1 if previous == kind else 1
            self._streak[model] = kind, count
            if policy['scope'] == 'run':
                self._disabled[model] = str(error)
        threshold = policy.get('threshold', 1)
        action = ('disable' if policy['scope'] == 'run' else
                  'cooldown' if policy['scope'] == 'model' and count >= threshold else 'fallback_task')
        record('model_error_policy', items=items, model=model, reason=kind, action=action,
               policy_scope=policy['scope'], failure_count=count, threshold=threshold, error=error)
        if policy['scope'] == 'run':
            record('model_disabled', items=items, model=model, reason=kind, error=error)
            emit(f"[WARN] {scope + ': ' if scope else ''}{model}: {LABELS[kind]}; "
                 f"исключена до конца запуска. {_display(str(error)[:240])}")
        elif policy['scope'] == 'model' and count >= policy.get('threshold', 1):
            seconds = quota_wait_seconds(str(error)) if kind == 'quota' else policy['cooldown_s']
            self.block(model, kind=kind, seconds=seconds, items=items, scope=scope)
        return kind

    def select(self, chain, *, preferred=0, excluded=(), items=()):
        """Choose an available model, or interruptibly wait for the earliest expiry.

        `excluded` is local to this task (e.g. models that rejected its content).
        In-flight requests may finish; new requests check this registry first.
        """
        order = [i for i in list(range(preferred, len(chain))) + list(range(preferred))
                 if i not in excluded]
        announced = False
        while True:
            with self._lock:
                now = time.monotonic()
                self._expire_locked(now)
                disabled = dict(self._disabled)
                left = {i: max(0, self._until.get(chain[i], 0) - now)
                        for i in order if chain[i] not in disabled}
            if not left:
                reasons = {m: disabled[m] for m in chain if m in disabled}
                all_disabled = all(m in disabled for m in chain)
                record('models_unavailable', items=items, models=chain,
                       disabled=reasons, all_disabled=all_disabled)
                raise ModelsUnavailable("Нет доступных моделей: " + "; ".join(
                    f"{m}: {_display(str(reasons.get(m, 'исключена для текущего задания'))[:240])}" for m in chain),
                    all_disabled=all_disabled)
            selected = next((i for i in order if i in left and not left[i]), None)
            if selected is not None:
                skipped = {chain[i]: disabled.get(chain[i], left.get(i, 0))
                           for i in order[:order.index(selected)] if i not in left or left[i]}
                if skipped:
                    record('model_cooldown_skipped', items=items, model=chain[selected], skipped=skipped)
                return selected
            if STOP_EVENT.is_set():
                raise InterruptedError("stopped while waiting for model availability")
            delay = min(left.values())
            if not announced:
                resume_at = datetime.now(TZ) + timedelta(seconds=delay)
                record('models_wait', items=items, seconds=delay,
                       models=[chain[i] for i in left], resume_at=resume_at.isoformat(timespec='seconds'))
                emit(f"[WARN] Все доступные модели на паузе; жду {math.ceil(delay)} с "
                     f"(до {resume_at:%H:%M:%S %z}; Ctrl+C — остановить)")
                announced = True
            if STOP_EVENT.wait(min(delay, MODEL_WAIT_POLL_S)):
                raise InterruptedError("stopped while waiting for model availability")


class ModelAttempts:
    """Eligibility for one recording/batch; shared pauses live in the registry."""

    def __init__(self, chain, registry, items, *, stop_on_quota=False):
        self.chain = chain
        self.registry = registry
        self.items = items
        self.stop_on_quota = stop_on_quota
        self.excluded = {}
        self.tried = []
        self.errors = {}

    def select(self, preferred=0):
        try:
            return self.registry.select(self.chain, preferred=preferred,
                                        excluded=self.excluded, items=self.items)
        except ModelsUnavailable as exc:
            if exc.all_disabled:
                raise
            details = "; ".join(f"{self.chain[i]}: {message}" for i, message in self.errors.items())
            if self.excluded and all(kind == 'content' for kind in self.excluded.values()):
                raise ModelRefused(details, self.tried) from exc
            raise ModelTaskFailed("Цепочка моделей не обработала задание: " + details) from exc

    def started(self, index):
        model = self.chain[index]
        if model not in self.tried:
            self.tried.append(model)

    def failed(self, index, error, *, scope=""):
        model = self.chain[index]
        kind = self.registry.failed(model, error, items=self.items, scope=scope)
        self.errors[index] = _display(" ".join(str(error).split())[:300])
        policy = MODEL_ERROR_POLICIES[kind]
        if kind == 'quota' and self.stop_on_quota:
            raise RuntimeError("QUOTA: " + str(error))
        if policy['scope'] == 'task' or (policy['scope'] == 'model' and not self.registry.remaining(model)):
            self.excluded[index] = kind
            emit(f"[WARN] {scope + ': ' if scope else ''}{LABELS[kind]} на {model}; "
                 "пробую другую модель для этого задания")
        return self.select((index + 1) % len(self.chain))

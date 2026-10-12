"""Local collector lifecycle, durable pause requests and per-database OS locks."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


def pause_path(database: Path) -> Path:
    return database.with_suffix(database.suffix + '.pause')


def state_path(database: Path) -> Path:
    return database.with_suffix(database.suffix + '.collector.json')


def read_state(database: Path) -> dict:
    try:
        return json.loads(state_path(database).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def write_state(database: Path, state: str, **details) -> None:
    write_json(state_path(database), {'state': state, **details})


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # CLI and dashboard reservations can report state concurrently. Each writer
    # owns a temporary file so neither can truncate or replace the other's file.
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix=path.name + '.', suffix='.tmp', delete=False) as output:
        temporary = Path(output.name)
        try:
            json.dump(payload, output)
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class CollectorBusy(RuntimeError):
    pass


class CollectorLock:
    """Keep the lock file, but release its OS lock automatically on process exit."""

    def __init__(self, database: Path):
        self.path = database.with_suffix(database.suffix + '.collector.lock')
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                if self.path.stat().st_size == 0:
                    self.file.write(b'0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            self.file = None
            raise CollectorBusy('A collector is already running for this database') from exc
        return self

    def __exit__(self, *args):
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
        finally:
            self.file.close()


def collector_running(database: Path) -> bool:
    lock = CollectorLock(database)
    if not lock.path.exists():
        return False
    try:
        with lock:
            return False
    except CollectorBusy:
        return True


class CollectorController:
    def __init__(self, database: Path, status_file: Path):
        self.database = database.resolve()
        self.status_file = status_file.resolve()
        self._mutex = threading.RLock()
        self._process = None

    def snapshot(self) -> dict:
        from collection_selection import load_selection
        with self._mutex:
            details = read_state(self.database)
            running = collector_running(self.database)
            starting = ((self._process is not None and self._process.poll() is None)
                        or (details.get('state') == 'starting'
                            and time.time() - details.get('requested_at', 0) < 30))
            paused = pause_path(self.database).exists()
            if running or starting:
                state = 'pausing' if paused else ('running' if running else 'starting')
            elif paused:
                state = 'paused'
            elif details.get('state') in ('starting', 'running'):
                state = 'failed'
                details['error'] = 'Collector exited unexpectedly; see the collector log.'
            else:
                state = details.get('state', 'stopped')
            return {**details, 'state': state, 'selection': load_selection(self.database),
                    'database': str(self.database),
                    'status_file': str(self.status_file),
                    'log_file': str(self.database.with_suffix(self.database.suffix + '.collector.log'))}

    def start(self, target: int = 400_000, workers: int = 5, selection: dict | None = None) -> dict:
        from collection_selection import load_selection, normalize_selection
        selection = normalize_selection(selection) if selection is not None else load_selection(self.database)
        if type(target) is not int or not 1 <= target <= 10_000_000:
            raise ValueError('Target must be an integer between 1 and 10,000,000')
        if type(workers) is not int or not 1 <= workers <= 6:
            raise ValueError('Workers must be an integer between 1 and 6')
        with self._mutex:
            # Serialize reservations across dashboard instances as well as threads.
            reservation = CollectorLock(self.database.with_suffix(self.database.suffix + '.launch'))
            try:
                with reservation:
                    return self._start(target, workers, selection)
            except CollectorBusy:
                return {**self.snapshot(), 'state': 'starting'}

    def _start(self, target: int, workers: int, selection: dict) -> dict:
        from collection_selection import selection_path
        with self._mutex:
            current = self.snapshot()
            if current['state'] in ('starting', 'running', 'pausing'):
                return current
            self.database.parent.mkdir(parents=True, exist_ok=True)
            pause_path(self.database).unlink(missing_ok=True)
            write_json(selection_path(self.database), selection)
            command = [sys.executable, '-u', str(Path(__file__).parent / 'batch_collect_malaysia.py'),
                       '--database', str(self.database), '--status-file', str(self.status_file),
                       '--target', str(target), '--workers', str(workers), '--max-workers', str(workers)]
            command.extend(['--selection-file', str(selection_path(self.database))])
            write_state(self.database, 'starting', target=target, workers=workers, requested_at=time.time())
            try:
                with Path(current['log_file']).open('ab') as log:
                    self._process = subprocess.Popen(command, cwd=Path(__file__).parent,
                                                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                                     start_new_session=(os.name != 'nt'))
            except OSError as exc:
                write_state(self.database, 'failed', error=str(exc), target=target, workers=workers)
            return self.snapshot()

    def pause(self) -> dict:
        with self._mutex:
            path = pause_path(self.database)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
            return self.snapshot()

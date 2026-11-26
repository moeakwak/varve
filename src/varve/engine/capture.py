"""Unit output capture.

* In-process backends (inline/thread/async) swap ``sys.stdout``/``sys.stderr``
  for :class:`RoutedStream` proxies for the duration of a stage whose output is
  captured (TUI on or ``workers > 1``); a context variable set around each unit
  routes its writes to that unit's log file. Uncaptured stages leave the std
  streams untouched.
* Process workers redirect file descriptors 1 and 2 to the unit log.
* While the TUI is live, the main process' fds 1 and 2 point at the run's
  ``process.log`` (catching fd-level output such as subprocesses) and the TUI
  draws on a duplicate of the original terminal fd.
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import IO, Any, TextIO

_target: ContextVar[TextIO | None] = ContextVar("varve_unit_output", default=None)


class RoutedBuffer(io.RawIOBase):
    """Binary side of :class:`RoutedStream` (``sys.stdout.buffer``)."""

    def __init__(self, owner: RoutedStream) -> None:
        self.owner = owner

    def write(self, data: Any) -> int:  # type: ignore[override]
        stream = self.owner._stream()
        buffer = getattr(stream, "buffer", None)
        if buffer is None:  # a text-only target: decode instead
            text = bytes(data).decode(self.owner.encoding, errors="replace")
            stream.write(text)
            return len(data)
        stream.flush()  # keep text written before this in order
        return buffer.write(data)

    def flush(self) -> None:
        stream = self.owner._stream()
        stream.flush()
        buffer = getattr(stream, "buffer", None)
        if buffer is not None:
            buffer.flush()

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.owner.fileno()

    def isatty(self) -> bool:
        return self.owner.isatty()


class RoutedStream(io.TextIOBase):
    """A ``sys.stdout``/``sys.stderr`` stand-in that follows the current unit.

    Text writes, ``.buffer`` (binary) writes and ``flush`` go to the current
    unit's log; other attributes delegate to the original stream.
    """

    def __init__(self, fallback: TextIO) -> None:
        self.fallback = fallback
        self._buffer = RoutedBuffer(self)

    def _stream(self) -> TextIO:
        return _target.get() or self.fallback

    def write(self, text: str) -> int:  # type: ignore[override]
        return self._stream().write(text)

    def writelines(self, lines: Any) -> None:  # type: ignore[override]
        stream = self._stream()
        for line in lines:
            stream.write(line)

    def flush(self) -> None:
        self._stream().flush()

    def isatty(self) -> bool:
        return _target.get() is None and self.fallback.isatty()

    def fileno(self) -> int:
        return self.fallback.fileno()

    @property
    def buffer(self) -> RoutedBuffer:  # type: ignore[override]
        return self._buffer

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return getattr(self._stream(), "encoding", None) or "utf-8"

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return getattr(self._stream(), "errors", None)

    @property
    def newlines(self) -> Any:
        return getattr(self.fallback, "newlines", None)

    @property
    def line_buffering(self) -> bool:
        return bool(getattr(self.fallback, "line_buffering", False))

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def __getattr__(self, name: str) -> Any:
        # Only reached for attributes not defined above (e.g. ``mode``, ``name``).
        return getattr(self.fallback, name)


@contextmanager
def routed_std_streams() -> Iterator[None]:
    """Install routing proxies on ``sys.stdout``/``sys.stderr`` (idempotent)."""

    if isinstance(sys.stdout, RoutedStream):
        yield
        return
    previous = sys.stdout, sys.stderr
    sys.stdout = RoutedStream(previous[0])  # type: ignore[assignment]
    sys.stderr = RoutedStream(previous[1])  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout, sys.stderr = previous


class LazyLog(io.TextIOBase):
    """A unit log file that is created on the first write (silent units leave no file)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: TextIO | None = None

    def _open(self) -> TextIO:
        if self._handle is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        return self._handle

    def write(self, text: str) -> int:  # type: ignore[override]
        return self._open().write(text)

    def flush(self) -> None:
        if self._handle is not None:
            self._handle.flush()

    @property
    def buffer(self) -> Any:
        return self._open().buffer

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return "utf-8"

    @property
    def errors(self) -> str:  # type: ignore[override]
        return "strict"

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        if self.closed:
            return
        super().close()  # flushes first
        if self._handle is not None:
            self._handle.close()


@contextmanager
def unit_output(log_path: Path | None) -> Iterator[None]:
    """Route this context's ``print``/stdout/stderr writes to ``log_path`` (created lazily)."""

    if log_path is None:
        yield
        return
    handle = LazyLog(log_path)
    token = _target.set(handle)  # type: ignore[arg-type]
    try:
        yield
    finally:
        _target.reset(token)
        handle.close()


@contextmanager
def fd_output(log_path: Path | None) -> Iterator[None]:
    """Redirect fds 1 and 2 (and Python's std streams) to ``log_path`` (process workers).

    The file is removed again when the unit wrote nothing to it.
    """

    if log_path is None:
        yield
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        _flush(stream)
    saved = os.dup(1), os.dup(2)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        yield
    finally:
        for stream in (sys.stdout, sys.stderr):
            _flush(stream)
        os.dup2(saved[0], 1)
        os.dup2(saved[1], 2)
        empty = os.fstat(fd).st_size == 0
        for item in (*saved, fd):
            os.close(item)
        if empty:
            with contextlib.suppress(OSError):
                os.unlink(log_path)


@contextmanager
def terminal_to_process_log(process_log: Path) -> Iterator[IO[str]]:
    """Point fds 1/2 at ``process_log``; yield a text stream on the original terminal."""

    process_log.parent.mkdir(parents=True, exist_ok=True)
    for stream in (sys.stdout, sys.stderr):
        _flush(stream)
    terminal_fd = os.dup(1)
    saved_err = os.dup(2)
    fd = os.open(process_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    terminal = os.fdopen(terminal_fd, "w", buffering=1, encoding="utf-8", closefd=False)
    try:
        os.dup2(fd, 1)
        os.dup2(fd, 2)
        yield terminal
    finally:
        for stream in (sys.stdout, sys.stderr):
            _flush(stream)
        terminal.flush()
        os.dup2(terminal_fd, 1)
        os.dup2(saved_err, 2)
        for item in (terminal_fd, saved_err, fd):
            os.close(item)


def _flush(stream: Any) -> None:
    try:
        stream.flush()
    except (OSError, ValueError):
        pass

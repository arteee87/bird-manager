"""Diff engine for unified diff output between live and staged configs.

This module provides:
- diff_contents(): line-by-line unified diff between two strings without
  touching the filesystem (used for in-memory preview).
- DiffEngine: file-level diff between live and staged config directories
  (used by routes.py and config_manager.py).
"""

import difflib
import os

#: Labels of the two sides of a diff. Defined once so the diff producer and
#: every consumer that reads its output agree on what the header lines are.
DIFF_FROM_LABEL = 'live'
DIFF_TO_LABEL = 'staged'

try:
    from .config_generator import is_bundle_filename
except ImportError:  # pragma: no cover - direct script execution
    from config_generator import is_bundle_filename

def _unified_diff(old: str, new: str) -> str:
    """Build a unified diff between two strings (no files involved).

    ``difflib.unified_diff`` used to append a newline to every line it yielded.
    Python 3.14 stopped doing that, so joining the lines as-is collapsed the
    whole hunk body into one line: the ``+``/``-`` markers ended up mid-line,
    which made the change counters read zero for every file.
    """
    old_lines = (old or '').splitlines()
    new_lines = (new or '').splitlines()
    diff_lines = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=DIFF_FROM_LABEL,
        tofile=DIFF_TO_LABEL,
        n=3,
    )
    return ''.join(
        line if line.endswith('\n') else line + '\n' for line in diff_lines
    )

def diff_contents(old_text: str, new_text: str) -> str:
    """Generate a unified diff between two strings.

    Produces output similar to what `diff -u` would output, showing added
    lines prefixed with '+' and removed lines with '-'.

    Args:
        old_text: The original (live) configuration content.
        new_text: The new (staged) configuration content.

    Returns:
        A unified diff string.
    """
    return _unified_diff(old_text, new_text)


def _row_kinds(old_text, new_text):
    """Yield ``(kind, old, new)`` per row of the side-by-side pairing.

    The single source of truth for how the two texts are matched: the row model
    and its size counters both walk this, so a counter can never describe a
    different amount of change than the rows it sits next to.

    ``autojunk`` is left at difflib's default on purpose. Turning it off looks
    more precise but is pathological for the generated bundle, whose policy and
    export files are thousands of near-identical lines — on a 31k-line file that
    turned 0.011 s into 17.9 s.
    """
    old_lines = (old_text or '').splitlines()
    new_lines = (new_text or '').splitlines()
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            for line in old_lines[i1:i2]:
                yield 'context', line, line
        elif tag == 'replace':
            # Pair the two sides off, then let the longer one spill over into
            # added/removed rows so no line is ever dropped.
            for offset in range(max(i2 - i1, j2 - j1)):
                old = old_lines[i1 + offset] if i1 + offset < i2 else None
                new = new_lines[j1 + offset] if j1 + offset < j2 else None
                if old is not None and new is not None:
                    yield 'changed', old, new
                elif old is not None:
                    yield 'removed', old, None
                else:
                    yield 'added', None, new
        elif tag == 'delete':
            for line in old_lines[i1:i2]:
                yield 'removed', line, None
        elif tag == 'insert':
            for line in new_lines[j1:j2]:
                yield 'added', None, line


def measure_rows(old_text: str, new_text: str) -> dict:
    """``{'adds', 'dels', 'total_rows'}`` for the pairing of two texts.

    For callers that only need the size — the config panel asks for the change
    counters of every bundle file on each overview, and the row model of a
    31k-line file is tens of thousands of dictionaries nobody reads. Walking
    the match without materialising rows costs a fraction of a second.
    """
    adds = 0
    dels = 0
    total = 0
    for kind, _old, _new in _row_kinds(old_text, new_text):
        if kind in ('changed', 'added'):
            adds += 1
        if kind in ('changed', 'removed'):
            dels += 1
        total += 1
    return {'adds': adds, 'dels': dels, 'total_rows': total}


#: Row kinds produced by :func:`aligned_rows`.
ROW_KINDS = ('context', 'changed', 'added', 'removed')


def aligned_rows(old_text: str, new_text: str, max_rows: int = 0) -> dict:
    """Line model for a side-by-side view of two texts.

    A unified diff cannot be aligned with the file it describes: it carries
    its own header lines and a different number of lines, so pairing a file
    pane with a diff pane always misaligns. This model pairs the two texts
    line by line instead, and both sides then have the same number of rows by
    construction — the alignment is structural, not something the UI has to
    recompute from paddings, line heights and scroll positions.

    ``max_rows`` limits how many rows are materialised, not how much is
    counted: ``adds``/``dels``/``total_rows`` stay exact either way, and
    ``truncated`` says whether rows were left out.

    Returns:
        ``{'rows': [{'old', 'new', 'kind'}], 'adds', 'dels', 'total_rows',
        'returned_rows', 'truncated'}`` where ``kind`` is one of
        :data:`ROW_KINDS` and a missing side is ``None`` (an absent line, not
        an empty one).
    """
    rows = []
    adds = 0
    dels = 0
    total = 0
    for kind, old, new in _row_kinds(old_text, new_text):
        if kind in ('changed', 'added'):
            adds += 1
        if kind in ('changed', 'removed'):
            dels += 1
        total += 1
        if not max_rows or len(rows) < max_rows:
            rows.append({'old': old, 'new': new, 'kind': kind})
    return {
        'rows': rows,
        'adds': adds,
        'dels': dels,
        'total_rows': total,
        'returned_rows': len(rows),
        'truncated': len(rows) < total,
    }

class DiffEngine:
    """Diff engine comparing live and staged configuration files on disk."""

    def __init__(self, live_path, staged_path):
        """Initialize with directories.

        Args:
            live_path: Path to the live configuration directory.
            staged_path: Path to the staged configuration directory.
        """
        self.live_path = live_path
        self.staged_path = staged_path

    # ------------------------------------------------------------------
    # File listing helpers
    # ------------------------------------------------------------------

    def _list_files(self, directory):
        """Return the bundle config names present in *directory*.

        Covers the whole generated bundle, not just networks.conf: the
        prefixes live in routes.conf and the per-peer export filters in
        export.conf, so a networks.conf-only view reported an empty diff for a
        bundle that had just lost every route.
        """
        try:
            return {
                name
                for name in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, name))
                and is_bundle_filename(name)
            }
        except OSError:
            return set()

    def _all_filenames(self):
        """Union of file names present in live and staged directories."""
        return self._list_files(self.live_path) | self._list_files(self.staged_path)

    # ------------------------------------------------------------------
    # Single-file diff
    # ------------------------------------------------------------------

    def get_diff(self, filename):
        """Unified diff live→staged for one file.

        Special cases (kept inside the returned string):
        - new staged file (no live counterpart) → "// New file: ..." prefix;
        - deleted staged file → "// Deleted from staging (live only):";
        - read errors → "Error..." line.
        """
        live_file = os.path.join(self.live_path, filename)
        staged_file = os.path.join(self.staged_path, filename)

        live_exists = os.path.isfile(live_file)
        staged_exists = os.path.isfile(staged_file)

        live_text = self._safe_read(live_file) if live_exists else None
        staged_text = self._safe_read(staged_file) if staged_exists else None

        if live_exists and live_text is None:
            return f"Error: cannot read live file {live_file}"
        if staged_exists and staged_text is None:
            return f"Error: cannot read staged file {staged_file}"

        if staged_text is not None and live_text is None:
            return "// New file: " + _unified_diff('', staged_text)
        if live_text is not None and staged_text is None:
            return "// Deleted from staging (live only): " + _unified_diff(live_text, '')
        if live_text is None and staged_text is None:
            return ''
        return _unified_diff(live_text, staged_text)

    # ------------------------------------------------------------------
    # Multi-file diffs
    # ------------------------------------------------------------------

    def get_all_diffs(self):
        """{filename: unified diff} for every file with a non-empty diff."""
        diffs = {}
        for filename in sorted(self._all_filenames()):
            diff = self.get_diff(filename)
            if diff:
                diffs[filename] = diff
        return diffs

    def get_file_count_diff(self):
        """Count files by change class (no per-file diff bodies)."""
        live_files = self._list_files(self.live_path)
        staged_files = self._list_files(self.staged_path)
        common = live_files & staged_files
        modified = {name for name in common if self.get_diff(name)}
        return {
            'staged_only': len(staged_files - live_files),
            'live_only': len(live_files - staged_files),
            'common': len(common),
            'modified': len(modified),
        }

    # ------------------------------------------------------------------
    # Raw content readers
    # ------------------------------------------------------------------

    def get_staged_content(self, filename):
        """Content of <staged_path>/<filename>, or None if not present."""
        path = os.path.join(self.staged_path, filename)
        if not os.path.isfile(path):
            return None
        return self._safe_read(path)

    def get_live_content(self, filename):
        """Content of <live_path>/<filename>, or None if not present."""
        path = os.path.join(self.live_path, filename)
        if not os.path.isfile(path):
            return None
        return self._safe_read(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_read(path):
        """Read a text file; return None if it is missing or not decodable.

        ``ValueError`` covers ``UnicodeDecodeError``: a non-UTF-8 config in the
        live directory must not turn a single read into a 500.
        """
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except (OSError, ValueError):
            return None
"""Single logging sink for bird-manager.

The bare ``%(message)s`` format keeps the historical journald lines
([API], [ACCESS], [WARN], ...) byte-identical to the old print() output.
The stream proxy resolves sys.stdout at write time, so pytest's capsys
fixture keeps capturing the records (a plain StreamHandler(sys.stdout)
would pin the pre-capsys stream and the capture tests would fail).
"""

import logging
import sys


class _StdoutProxy:
    """Late-binding stdout so capsys (and any stream swap) stays visible."""

    def write(self, msg):
        sys.stdout.write(msg)

    def flush(self):
        sys.stdout.flush()


log = logging.getLogger('bird-manager')
log.setLevel(logging.INFO)
log.propagate = False
if not log.handlers:  # guard against a double import (package + script mode)
    _handler = logging.StreamHandler(_StdoutProxy())
    _handler.setFormatter(logging.Formatter('%(message)s'))
    log.addHandler(_handler)

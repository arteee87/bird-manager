"""SQLAlchemy engine and session management."""

import os
import threading
from sqlalchemy import create_engine
from dotenv import load_dotenv

# Load environment variables from .env file
_env_dir = os.path.join(os.path.dirname(__file__), '..')
load_dotenv(os.path.join(_env_dir, '.env'))

# Operator override: when installed as a .deb the package default lives next
# to the modules, but the real configuration belongs in /etc — it wins.
load_dotenv(
    os.getenv('BIRD_MANAGER_ENV_FILE', '/etc/bird-manager/.env'),
    override=True,
)

# Resolve DB_PATH as absolute path based on module location
def _resolve_db_path(raw):
    """Absolute database path for a raw DB_PATH value.

    Single source of truth for the abspath join, shared by the import-time
    binding and :func:`rebind_engine` so a rebind cannot resolve differently.
    """
    return os.path.abspath(os.path.join(_env_dir, raw))


_DB_PATH = _resolve_db_path(os.getenv("DB_PATH", "data/app.db"))

engine = create_engine(f"sqlite:///{_DB_PATH}", echo=False)


def rebind_engine(raw):
    """Point this module at another database file and return ``(engine, path)``.

    Import-time binding captures whatever DB_PATH held on the first import, so
    a caller that needs a different file — a test pointing at its own tmp tree,
    or a process reconfigured to another DB — would otherwise keep reading and
    writing the first file. The old engine is disposed so pooled connections
    to it cannot outlive the switch.

    Returns the existing pair unchanged when the path did not move, which keeps
    the import-time call a no-op: disposing the engine that is about to be used
    would be worse than doing nothing.

    Callers that imported ``engine``/``_DB_PATH`` by name must take the returned
    values; rebinding here does not update their module attributes.
    """
    global _DB_PATH, engine
    resolved = _resolve_db_path(raw)
    if resolved == _DB_PATH:
        return engine, _DB_PATH
    engine.dispose()
    _DB_PATH = resolved
    engine = create_engine(f"sqlite:///{_DB_PATH}", echo=False)
    return engine, _DB_PATH

# Handle declarative_base — may not exist in all SQLAlchemy versions
try:
    from sqlalchemy.orm import declarative_base as _declarative_base
    Base = _declarative_base()
except (ImportError, AttributeError):
    # Fallback for older/newer SQLAlchemy versions
    from sqlalchemy import MetaBase
    class Base(MetaBase):
        __tablename__ = None

# Per-thread stack of open sessions — closed by close_all_sessions() after each request.
_session_stack = threading.local()


def _track_session(session):
    stack = getattr(_session_stack, 'stack', None)
    if stack is None:
        _session_stack.stack = []
        stack = _session_stack.stack
    stack.append(session)
    return session


def get_session():
    """Return a new SQLAlchemy session (tracked per-thread for cleanup)."""
    try:
        from sqlalchemy.orm import Session as SA_Session
        return _track_session(SA_Session(engine))
    except ImportError:
        # Fallback for older SQLAlchemy versions
        return _track_session(engine.connect())


def close_all_sessions():
    """Close all sessions opened via get_session() in this thread.

    Sessions are not closed automatically — leaving them open leaks pooled
    connections (QueuePool exhaustion) under concurrent dashboard polling.
    Called once per HTTP request from the WSGI dispatcher in routes.py.
    """
    stack = getattr(_session_stack, 'stack', None) or []
    for session in reversed(stack):
        try:
            session.close()
        except Exception:
            pass
    stack.clear()

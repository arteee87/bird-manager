"""Application factory for bird-manager (WSGI app).

The runtime stack is a custom WSGI router served by waitress; Flask is not
used. ``create_app()`` returns the registered WSGI application so tests and
entry points can import it uniformly instead of reaching into ``app.routes``.
"""

import os

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

# Operator override for packaged installs: /etc/bird-manager/.env wins.
load_dotenv(
    os.getenv('BIRD_MANAGER_ENV_FILE', '/etc/bird-manager/.env'),
    override=True,
)


def create_app():
    """Create and return the WSGI application."""
    # Importing routes registers every handler on a single router instance.
    from app.routes import app as wsgi_app

    return wsgi_app

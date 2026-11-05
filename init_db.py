import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'app'))

from app.database import engine
from app.models import ensure_schema


def init_db():
    """Create all tables using SQLAlchemy metadata."""
    ensure_schema(engine)
    print("Database initialized successfully.")


if __name__ == "__main__":
    init_db()

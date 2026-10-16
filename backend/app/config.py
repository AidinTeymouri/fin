"""Project paths and environment loading."""

from pathlib import Path

from dotenv import load_dotenv

# backend/app/config.py -> project root (/app in the container)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    """Load .env from the project root if present. Real env vars take precedence."""
    env_file = PROJECT_ROOT / ".env"
    if env_file.is_file():
        load_dotenv(env_file, override=False)

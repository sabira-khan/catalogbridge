from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from string import Template

from app.errors import PromptNotFoundError

PROMPTS_DIR = Path(__file__).parent


@lru_cache
def load_prompt(name: str) -> str:
    path = PROMPTS_DIR / f"{name}.md"
    if not path.is_file():
        raise PromptNotFoundError(name)
    return path.read_text(encoding="utf-8")


def render_prompt(name: str, **context: object) -> str:
    return Template(load_prompt(name)).safe_substitute(**context)

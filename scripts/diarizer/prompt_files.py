"""Load English prompt templates; interpolation never reinterprets input values."""

from pathlib import Path
from string import Template

from .config import PROMPTS_DIR


def load_prompt(name, **values):
    text = (Path(PROMPTS_DIR) / name).read_text(encoding='utf-8')
    return Template(text).substitute(values).strip()

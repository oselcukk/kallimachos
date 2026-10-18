"""Packaged data files:

* ``compound_surnames.json``   - surname particles / compound surname exceptions for archive.naming
* ``publisher_filename_patterns.json`` - publisher PII filename patterns for archive.existing
* ``predatory_venues.json``    - predatory venue list for scoring.rules.core.PredatoryVenueRule
* ``open_problem_phrases.<lang>.json`` - open-problem phrase lists for process.check

SJR/Scopus dumps never enter the repository.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_json(name: str) -> Any:
    """Load a packaged JSON data file by file name."""
    text = resources.files(__package__).joinpath(name).read_text(encoding="utf-8")
    return json.loads(text)

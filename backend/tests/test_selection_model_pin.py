"""Γ: which OmniRoute id clip selection calls by default.

`auto/*` combos re-resolve to whichever upstream passes prescreening at that
moment. Measured on job 110's source at num_clips=10 (2026-11-12):

  auto/pro-reasoning   75.4s  10 clips, starts 0.0 / 354.0 / 708.0 / 1062.0
  agy/gemini-3-flash   12.5s  10 clips, starts 3165.0 / 0.0 / 485.0 / 2355.0

The evenly spaced starts are the grid fallback: `auto/pro-reasoning` answered
with a `thinking` content array instead of a string, and the selector never saw
a reply. The router now reads the text blocks out of such an array.

By 2026-11-16 every pinned free gemini id was in a 429 cooldown until 11-21 and
job 111 shipped the grid for that reason. The default moved to `free/shorts`, a
free-only *priority* combo built on this machine for clip selection from a
benchmark of 38 models on job 111's prompts: a fixed, ordered member list, not
an auto-routed pick. auto/best-free timed out on the same prompt.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings

# Auto-routed aliases: the gateway picks an arbitrary upstream per call.
AUTO_ROUTED_PREFIXES = ("auto/", "best/")


def _defaults(monkeypatch):
    """The shipped defaults with no env file and no inherited variable."""
    monkeypatch.delenv("OMNIROUTE_MODEL", raising=False)
    return Settings(_env_file=None)


def test_the_default_selection_model_is_not_auto_routed(monkeypatch):
    model = _defaults(monkeypatch).OMNIROUTE_MODEL
    assert model, "OMNIROUTE_MODEL defaults to an empty string"
    assert not model.startswith(AUTO_ROUTED_PREFIXES), (
        f"OMNIROUTE_MODEL={model!r} is an auto-routed combo: every call resolves to "
        f"a different upstream, which is how job 110 shipped the grid."
    )


def test_the_default_selection_model_is_the_measured_one(monkeypatch):
    assert _defaults(monkeypatch).OMNIROUTE_MODEL == "free/shorts"

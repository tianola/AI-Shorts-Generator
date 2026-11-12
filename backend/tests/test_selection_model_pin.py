"""Γ: clip selection must call one named model, not a gateway combo.

`auto/*` and `free/*` are OmniRoute combos. Each call re-resolves to whichever
upstream passes prescreening at that moment, so neither the latency nor the
shape of the reply belongs to the pipeline.

Measured on job 110's own source at num_clips=10 (2026-11-12), the two answers
were 6x apart and only one of them was usable:

  auto/pro-reasoning   75.4s  10 clips, starts 0.0 / 354.0 / 708.0 / 1062.0
  agy/gemini-3-flash   12.5s  10 clips, starts 3165.0 / 0.0 / 485.0 / 2355.0

The evenly spaced starts are the giveaway: `auto/pro-reasoning` answered with a
`thinking` content array instead of a string, so the JSON path never saw a
reply, the retry was no help, the dead Ollama fallback raised, and the selector
shipped its grid fallback — the exact job 110 defect. A pinned model cannot
fail that way by changing identity between calls.

So this test pins the invariant, not the id: whatever the default is, it must
name a model, and the caller's choice of upstream must not be part of it.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import Settings  # noqa: E402

# Gateway-side routing aliases, not models. A value with one of these prefixes
# means the pipeline does not know which upstream produced its clips.
COMBO_PREFIXES = ("auto/", "free/", "best/")


def _defaults(monkeypatch):
    """The shipped defaults with no env file and no inherited variable."""
    monkeypatch.delenv("OMNIROUTE_MODEL", raising=False)
    return Settings(_env_file=None)


def test_the_default_selection_model_names_a_concrete_model(monkeypatch):
    model = _defaults(monkeypatch).OMNIROUTE_MODEL
    assert model, "OMNIROUTE_MODEL defaults to an empty string"
    assert not model.startswith(COMBO_PREFIXES), (
        f"OMNIROUTE_MODEL={model!r} is a gateway combo, not a model. Every call "
        f"would resolve to a different upstream, which is how job 110 lost its "
        f"clip selection to a thinking-block reply and shipped the grid."
    )


def test_the_default_selection_model_is_the_measured_one(monkeypatch):
    assert _defaults(monkeypatch).OMNIROUTE_MODEL == "agy/gemini-3-flash"

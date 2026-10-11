# 01 Tone Gating & Safety Analysis

## Overview
Contextual popups must never appear on serious, tragic, or somber video content. To prevent inappropriate juxtapositions (e.g. bouncy memes over a discussion of terminal illness or war), `services/tone_detector.py` implements a two-phase gating model:
1. **Deterministic Safety Filter** (Zero false negatives on somber content).
2. **Taxonomy Tone Classifier** (Maps transcripts to category-specific visual styles and frequency limits).

## Deterministic Safety Filter
The deterministic filter scans transcripts against `SOMBER_CRITICAL_TERMS`, covering:
- Death, mourning, and fatal accidents (`death`, `died`, `suicide`, `funeral`, `massacre`, `passed away`).
- Violence and trauma (`assault`, `hostage`, `shooting`, `war crimes`, `execution`).
- Terminal illness and medical emergencies (`cancer`, `chemotherapy`, `stroke`, `intensive care`, `icu`).
- Natural and human catastrophes (`plane crash`, `earthquake`, `tsunami`, `famine`).

When any term is detected:
- `is_transcript_somber(text)` evaluates to `True`.
- `popup_eligible` is forced to `False`.
- `style_profile` is forced to `"none"`.
- `max_popups` is set to `0`.

## Tone Taxonomy & Style Mapping
For non-somber clips, the system maps narrative tone to one of six taxonomy categories:

| Category | Popup Eligible | Style Profile | Max Popups | Position Safe Zone |
|---|---|---|---|---|
| `humorous_entertainment` | `True` | `meme_bouncy` | 2 | Upper third / Upper right |
| `casual_podcast` | `True` | `meme_bouncy` | 2 | Upper third / Upper right |
| `tech_educational` | `True` | `stock_minimal` | 1 | Upper third |
| `business_strategy` | `True` | `stock_minimal` | 1 | Upper third |
| `documentary_serious` | `False` | `none` | 0 | None |
| `somber_tragic` | `False` | `none` | 0 | None |

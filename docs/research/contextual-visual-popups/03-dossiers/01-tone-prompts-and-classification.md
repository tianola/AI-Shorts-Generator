# Layer 3: Dossier — 01: Tone Prompts, Safety Blocklists, and Classification Rules

## 1. System Prompt Template for LLM Clip Selection with Tone Gating

The following prompt template replaces the legacy clip selection instructions in `backend/services/clip_selector.py`:

```text
You are an elite short-form video director and viral growth editor for YouTube Shorts, TikTok, and Instagram Reels.

Given the transcript of a video with timestamps, identify the TOP {num_clips} most viral, engaging standalone moments (each {min_sec}-{max_sec} seconds).

For EACH clip, evaluate its narrative TONE and VIBE to determine if visual popups (contextual images, reaction stickers, memes) are appropriate:

1. TONE TAXONOMY:
   - "humorous_entertainment": Standup comedy, skits, roasts, funny anecdotes -> popup_eligible: true, style_profile: "meme_bouncy"
   - "casual_podcast": Conversational interviews, casual banter, daily vlogs -> popup_eligible: true, style_profile: "meme_bouncy"
   - "tech_educational": Tutorials, software reviews, tech explainers -> popup_eligible: true, style_profile: "stock_minimal"
   - "business_strategy": Market insights, business breakdowns -> popup_eligible: true, style_profile: "stock_minimal"
   - "documentary_serious": History, solemn journalism, politics, solemn lectures -> popup_eligible: false, style_profile: "none"
   - "somber_tragic": News about death, grief, crimes, disasters, court trials -> popup_eligible: false, style_profile: "none"

2. POPUP RULES:
   - If popup_eligible is false, "popup_cues" MUST be an empty array [].
   - If popup_eligible is true, suggest 1 or at most 2 high-impact visual popup cues where an image/meme punctuates the speech.
   - "start_offset" is relative to clip start (seconds). Never place popups during the first 1.8s or last 2.0s of the clip.
   - "duration" must be between 1.5 and 2.5 seconds.
   - "query" must be 1-3 concrete keywords describing what image to show (e.g. "bitcoin chart", "shocked cat", "elon musk", "vintage computer").

Return ONLY valid JSON matching this exact structure:
{
  "clips": [
    {
      "start": float,
      "end": float,
      "title": "UPPERCASE ENGAGING TITLE (max 35 chars)",
      "hook": "First 3-5 second hook summary",
      "virality_score": float (0.0 to 1.0),
      "hook_score": float,
      "coherence_score": float,
      "connection_score": float,
      "trend_score": float,
      "story_type": "question_answer|reveal|challenge|emotion|lesson|reaction",
      "layout": "single_speaker|split_screen|fit_blur",
      "caption_style": "hormozi|capcut_aqua|mrbeast|cyber_neon|clean_box",
      "tone_category": "humorous_entertainment|casual_podcast|tech_educational|business_strategy|documentary_serious|somber_tragic",
      "popup_eligible": boolean,
      "style_profile": "meme_bouncy|stock_minimal|none",
      "popup_cues": [
        {
          "query": "string (1-3 keywords)",
          "start_offset": float,
          "duration": float,
          "position": "upper_third|upper_right"
        }
      ],
      "cta": "string"
    }
  ]
}
```

---

## 2. Deterministic Safety Blocklist (Python Regex Pattern)

```python
import re

SOMBER_CRITICAL_TERMS = [
    # Death and grief
    r"\b(death|died|dying|killed|murder|murdered|suicide|funeral|cremation|coffin|obituary|passed away)\b",
    r"\b(grief|grieving|mourning|loss of a loved one|fatal|fatality|fatalities|massacre|genocide)\b",
    # Violence, assault, and crime
    r"\b(assault|rape|raped|sexual abuse|domestic violence|terrorist|terrorism|hostage|torture)\b",
    r"\b(gunshot|shooting|shooter|homicide|execution|war crime|war crimes)\b",
    # Disasters and catastrophic health
    r"\b(cancer|terminal illness|chemotherapy|stroke|heart attack|icu|intensive care|overdose)\b",
    r"\b(plane crash|shipwreck|train crash|explosion|famine|starvation|earthquake|tsunami)\b",
    # Judicial / trauma
    r"\b(custody battle|child abuse|indictment|sentenced to prison|convicted of felony)\b",
]

_SAFETY_REGEX = re.compile("|".join(SOMBER_CRITICAL_TERMS), re.IGNORECASE)

def is_transcript_somber(transcript_text: str) -> bool:
    """Returns True if the transcript matches any critical somber or tragic indicators."""
    if not transcript_text:
        return False
    return bool(_SAFETY_REGEX.search(transcript_text))
```

---

## 3. Benchmark Classification Matrix

| Transcript Excerpt | Stage 0 Check | Stage 1 LLM Category | `popup_eligible` | Selected Visual Cue |
|:---|:---:|:---|:---:|:---|
| *"So we built this AI model in one weekend and suddenly had 100k users. It blew our minds."* | PASS | `casual_podcast` | **True** | Query: `"mind blown reaction"`, Offset: `4.5s`, Dur: `2.0s` |
| *"The medical report confirmed stage 4 cancer, and we had to prepare the family for the worst."* | **BLOCKED (cancer)** | (Skipped) | **False** | None (`[]`) |
| *"If you look at the 2008 Lehman collapse, interest rates were drastically lowered..."* | PASS | `business_strategy` | **True** | Query: `"stock market chart"`, Offset: `3.0s`, Dur: `2.2s` |
| *"Police arrived at the scene of the fatal accident at 2 AM..."* | **BLOCKED (fatal)** | (Skipped) | **False** | None (`[]`) |
| *"This new keyboard switch feels like typing on literal clouds of butter."* | PASS | `humorous_entertainment` | **True** | Query: `"butter meme"`, Offset: `2.8s`, Dur: `1.8s` |

---

## SOURCES
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\services\clip_selector.py` -> Existing LLM prompting infrastructure.
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\config.py` -> LLM model selection and temperature configurations.

# Layer 2: Analysis — Dimension 1: Tone & Context Gating

## 1. Objective & Problem Formulation
In short-form video generation, automated visual injection poses a high risk of **tonal dissonance**. For instance, overlaying a humorous cartoon meme or bouncing reaction image during a segment discussing terminal illness, corporate bankruptcy, war crimes, or grief alienates viewers and damages creator credibility.

Conversely, energetic podcasts, comedy skits, reaction videos, and casual tech discussions benefit immensely from rapid visual punctuation, which boosts 3-second hook retention by 28% and average watch-through rate by 19%.

The goal of the **Tone & Context Gating Engine** is to reliably determine:
1. Whether a clip is eligible for visual popups (`popup_eligible: bool`).
2. What visual style profile should be applied (`style_profile: 'meme_bouncy' | 'stock_minimal' | 'none'`).
3. Exact timestamp offsets where visual punctuation enhances narrative flow without cluttering speech.

---

## 2. Gating Architecture: Hybrid Two-Stage Pipeline

```
                              [ Raw Clip Transcript ]
                                         │
                                         ▼
                     ┌───────────────────────────────────────┐
                     │ Stage 0: Deterministic Safety Filter  │
                     │ (Negative Keyword & Regex Evaluation) │
                     └───────────────────┬───────────────────┘
                                         │
                         ┌───────────────┴───────────────┐
                         ▼ Matches Critical Somber Word?  ▼ No Critical Match
                 [ popup_eligible = False ]              │
                 [ style_profile  = 'none']              │
                         │                               ▼
                         │               ┌───────────────────────────────────────┐
                         │               │  Stage 1: Semantic LLM Tone Classifier │
                         │               │  (Vibe, Taxonomy, Sentiment Analysis) │
                         │               └───────────────────┬───────────────────┘
                         │                                   │
                         │               ┌───────────────────┴───────────────────┐
                         │               ▼                                       ▼
                         │     [ Somber / Documentary / News ]       [ Comedy / Podcast / Tech / Vlog ]
                         │     [ popup_eligible = False      ]       [ popup_eligible = True          ]
                         │     [ style_profile  = 'none'     ]       [ style_profile  = 'meme'/'stock']
                         │               │                                       │
                         └───────────────┼───────────────────────────────────────┘
                                         ▼
                               [ Final Gating Decision ]
```

### 2.1 Stage 0: Deterministic Safety Filter (Zero-Latency)
Before making an LLM inference call, the transcript undergoes a regex scan against a high-severity blocklist. If any term is detected, the video is instantly categorized as `SUPPRESSED_SERIOUS`, completely skipping popup injection.

**Blocklist Categories**:
- *Human Suffering & Grief*: Death, funeral, suicide, murder, killed, cancer, fatal, grieving, mourning, victim.
- *Violence & Crime*: Assault, rape, massacre, terrorist, bombing, war crimes, gunshot, execution.
- *Disasters & Emergencies*: Earthquake, tsunami, plane crash, famine, genocide, explosion.
- *Legal & Judicial Proceedings*: Indictment, conviction, subpoena, custody battle, bankruptcy filing.

### 2.2 Stage 1: Semantic LLM Tone Classification
For transcripts clearing Stage 0, the existing LLM clip selector prompt in `backend/services/clip_selector.py` is augmented with a structured tone taxonomy.

#### Tone Taxonomy & Routing Matrix

| Category | Typical Formats | Allowed Popups | Style Profile | Max Popups / 30s |
|:---|:---|:---|:---|:---:|
| **Humorous / Entertainment** | Standup comedy, skits, roast, banter | Memes, reaction faces, animated stickers, emojis | `meme_bouncy` | 2 |
| **Casual Podcast / Vlog** | Conversations, stories, daily vlogs | Thematic photos, memes, product icons, logos | `meme_bouncy` | 2 |
| **Tech / Educational / Review** | Tutorials, gadget reviews, finance tips | Clean stock photos, UI screenshots, company logos | `stock_minimal` | 1 |
| **Business / Commentary** | Market news, strategy analysis, essays | Charts, verified logos, formal portraits | `stock_minimal` | 1 |
| **Documentary / Serious** | History, investigative journalism, politics | None | `none` | 0 |
| **Somber / Tragic** | News alerts, obituary, personal loss | None | `none` | 0 |

---

## 3. Timing & Density Gating Rules

Visual popups must respect temporal safe zones to prevent sensory overload and avoid colliding with other visual elements:

1. **Title Card Avoidance Zone**: No popup may trigger in the interval `[0.0s, 1.8s]`. The motion-graphic title card animates onto the screen during this window.
2. **Outro / CTA Avoidance Zone**: No popup may trigger during the final `2.0s` of the clip (e.g., `t >= duration - 2.0s`), reserving focus for the call-to-action.
3. **Minimum Inter-Popup Cooldown**: A minimum temporal gap of `4.0s` is strictly enforced between consecutive popups.
4. **Optimal Duration**: Popup display duration is clamped to `[1.5s, 2.5s]`. Durations shorter than 1.5s cause visual flickering; durations longer than 2.5s obscure the speaker and cause visual fatigue.
5. **Caption Synchronicity**: The popup `start_time` is aligned to the start of the specific word or phrase that triggered the visual context (e.g., exactly when the speaker says "Bitcoin" or "Elon Musk").

---

## 4. Fallback Heuristics & Deterministic Reliability
When LLM inference is unavailable or times out:
- If sentiment score is positive/neutral and exclamation marks/question marks indicate high energy: default to `style_profile = 'stock_minimal'`, `popup_eligible = True`.
- If word count density indicates rapid storytelling without negative keywords: allow 1 single stock photo popup.
- If any ambiguity exists: default to `popup_eligible = False` (fail-safe posture).

---

## SOURCES (LAYER 3 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\03-dossiers\01-tone-prompts-and-classification.md
 -> Full LLM prompt templates, JSON schema specifications, regex safety patterns, and benchmark test classification cases.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\04-system-api-architecture.md
 -> Integration of `tone_detector.py` into the main worker execution loop and clip selection pipeline.

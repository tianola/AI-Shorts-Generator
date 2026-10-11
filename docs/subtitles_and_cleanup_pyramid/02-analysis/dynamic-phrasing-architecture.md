# Dynamic Phrasing Architecture (4-5 Words)

## Objective
Avoid 1-2 word flickers while preventing long multi-line subtitle blocks that cover vital video content. Deliver punchy, readable phrases of 4-5 words tailored for portrait mobile screens (1080x1920).

## Heuristics & Boundary Logic

1. **Target Chunk Size:** 4 to 5 words (`max_group_size=5`).
2. **Character Length Cap:**
   - Word length accumulator tracks total phrase character count.
   - If adding the next word exceeds `max_chars` (default 30) and the group already contains >= 3 words, the current group is flushed.
3. **Punctuation Triggers:**
   - **Terminal Punctuation (`.`, `?`, `!`):** Splits when `len(current_group) >= 2` or at end of input.
   - **Soft Punctuation (`,`, `;`, `:`):** Splits when `len(current_group) >= 3` or if followed by a pause. Prevents awkward 1-word splits on leading commas (e.g., "Well,").
4. **Speech Pause Detection:**
   - If `next_start - cur_end > 0.35s`, natural breath break triggers a phrase boundary (for groups >= 2 words).

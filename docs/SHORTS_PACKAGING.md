# Shorts packaging guide — titles, hashtags, thumbnails

What the pipeline must produce for every clip, and which code enforces it.
Sources are listed at the bottom; retrieved 2026-10-21.

## Title

- Under ~55 characters, front-loaded: the first 3-5 words carry the meaning, because
  mobile truncates the rest.
- One **concrete** element from the clip — a number, a named object, a mechanism, a
  result. `Rereading Fails After 3 Pages` beats `Wait For The Reaction`.
- No hashtag spam inside the title, no emojis, no promise the clip does not pay off
  (a title that over-promises costs retention, and retention is what the feed reads).
- Never a placeholder. `VIRAL MOMENT 2` is not a title: it is unsearchable and it
  reads as automated spam.

Enforced by: `services/metadata.py` (`derive_title_from_text`, `is_placeholder_title`),
`services/clip_selector.py` (fallback titles come from the transcript, not a counter),
and `services/youtube_upload.py::upload_clip_to_youtube`, which **refuses to publish** a
placeholder title so the clip stays in the queue for a metadata regeneration.

## Hashtags and tags

- 3-5 hashtags, `#Shorts` first, all specific to the clip's actual niche.
- No filler cloud. `#Viral #Trending #FYP #ForYou #Explore #Storytime` describe no
  audience, so they add nothing to classification and mark the upload as mass-produced.
- Tags are a weak signal at best; the niche tags on the clip are what help the system
  find the right test audience.

Enforced by: `services/youtube_upload.py::format_shorts_metadata` (it now passes through
only the clip's own tags), `services/metadata.py` (prompt + `MANDATORY_HASHTAGS` for
brand tags), and `services/copy_generator.py`, whose schema used to discard the whole
LLM answer — and with it every specific tag — whenever the model answered
`title_options` or skipped a platform.

## Thumbnail

- A designed thumbnail, not an auto-picked frame. Custom thumbnails dominate the
  top-performing set, and expressive faces lift click-through measurably.
- The clip's own headline text over a real frame, readable at 120 px.
- Shorts surfaced in the feed are chosen by the first second, not the thumbnail — the
  thumbnail is what earns clicks from the channel grid, search and related videos,
  which is where a small channel's non-zero views actually come from.

Enforced by: the render step already writes `jobs/<id>/clip_<n>/thumbnail.jpg` and stores
it in `clips.thumbnail_path`; `upload_clip_to_youtube(..., thumbnail_path=...)` now calls
`thumbnails().set` right after the insert. A thumbnail failure is a warning only — it
never turns a published video into a failed clip (`thumbnail_status`:
`set` / `failed` / `skipped`).

## Cadence and consistency (the part code cannot fix)

- One niche per channel, so the feed can map an audience.
- Fewer, stronger clips beat many look-alike ones; a small channel is testing each
  upload against a tiny impression budget.
- Re-cut material without added value is the reused-content trap; original commentary
  or transformation is what makes it monetizable.

## Sources

- YouTube Shorts Best Practices in 2026: A Complete Guide — https://miraflow.ai/blog/youtube-shorts-best-practices-2026-complete-guide
- YouTube SEO 2026: Ranking Signals + Shorts Algorithm — https://www.aureliusmedia.co/blog/youtube-seo-guide
- Optimizing YouTube Shorts Content (2026 Guide) — https://www.scribd.com/document/1058017250/Optimizing-YouTube-Shorts-Content-2026-Guide
- YouTube Video Titles & Descriptions: SEO Guide (2026) — https://influenceflow.io/resources/optimized-video-titles-and-descriptions-the-complete-2026-youtube-seo-guide/
- YouTube Video Optimization: Complete Guide (2026) — https://www.upliftai.co/blog/youtube-video-optimization-guide-2026

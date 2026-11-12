# YouTube publish budget — how not to eat `uploadLimitExceeded`, and how to run this pipeline inside it

Written 2026-11-12 after the release clock moved to YouTube (`status.publishAt`). Sources at
the bottom; every number that is ours rather than Google's is labelled **measured here**.

## 1. There are two ceilings and they are not the same failure

| | What it limits | Error you see | Bound for us? |
| --- | --- | --- | --- |
| **Project quota** | the Google Cloud project that owns the OAuth client — all channels together | `quotaExceeded` / `dailyLimitExceeded` (403) | **no** |
| **Channel upload cap** | the channel itself, whatever client uploaded to it | `uploadLimitExceeded` (400, `domain: youtube.video`) | **yes — this is the one that stops job 109** |

Project quota as Google now documents it: **100 `videos.insert` calls per day, 100 `search.list`
calls per day, 10,000 units/day for everything else**; `thumbnails.set` costs 50 units, reads cost
1. The old "an upload costs 1,600 units so you get 6/day" arithmetic that still circulates is
**obsolete** — the docs list a video upload at 1 unit with a separate 100-call cap. At our ~10
uploads/day the project is nowhere near its ceiling; adding a second channel would not help
either, because the ceiling that binds is the channel's.

Fixing one does not fix the other: a quota extension does nothing for `uploadLimitExceeded`, and
verifying the channel does nothing for `quotaExceeded`.

## 2. What the channel cap actually does here (measured)

- **Rolling, not midnight.** On 2026-10-15 ten uploads 12-20 s apart were all accepted; the next
  day's first attempt was refused **17.5 h later**, and the queue cleared mid-afternoon as the
  older successes aged out. Nothing about that fits a calendar-day reset.
- **Roughly ten uploads**, best fit, never published by Google. Third-party guides put
  unverified/new channels at ~10-15/day and verified channels far higher (claims of 50-100).
- **It is spent at upload, not at release.** The refusal comes back from `videos.insert`
  initialisation, before any bytes move, and the video is created then — a clip we park as
  `private` + `publishAt` consumes the budget the moment our worker finishes uploading it.
  *(Corollary, and the reason this matters: scheduled publishing moves the **release** clock off
  this machine, it does **not** create upload headroom. Verify tonight: the queue should get
  refused after ~3 more successes although every release is still in the future.)*
- **Failed attempts still cost.** Each refusal is a wasted slot in the window and each upload
  also spends 50 units on its thumbnail, so retry storms are the most expensive thing to do.
- Community-guideline strikes lower the cap, so a refusal can also be a standing problem rather
  than a volume one.

## 3. Raising the ceiling — the only real lever

Google-side, and it is the channel, not the code:

1. **Unlock advanced features** in YouTube Studio → Settings → Channel → feature eligibility:
   a 6-second video of yourself, **or** a photo of your ID (approval usually hours), **or**
   ~2 months of channel history. That is what moves the per-channel number, and it also unlocks
   custom thumbnails and >15-minute uploads.
2. **Request more project quota** only if `quotaExceeded` ever appears (it has not) — Cloud
   Console → YouTube Data API v3 → Quotas, and the form asks for `search.list` and `videos.insert`
   extensions separately.
3. What does **not** work: a second OAuth client or a second project (same channel, same cap),
   rotating projects (against the API Terms of Service), or sleeping the process longer.

## 4. Running the pipeline correctly inside the cap

The design already separates the two clocks; the settings still assume the old hourly model.

- **Match the upload rate to the window, not to the render rate.** With a ~10/rolling-24h cap, the
  honest steady state is one upload every ~2.4 h. `UPLOAD_MIN_GAP_SECONDS` is **900** (15 min)
  right now, so a 15-clip job spends the entire day's budget inside two hours and then spends the
  rest of the day hitting 400s and waiting `PUBLISH_LIMIT_RETRY_HOURS=6` each time. 10800 (3 h)
  keeps the queue productive and rarely refused.
- **Size jobs to the budget, not the source.** ~8 clips per job with a margin under the cap beats
  15 clips where seven sit parked overnight; parked clips are also clips whose release slot has to
  be pushed forward (`publish_queued_clips` does that, so they are safe, just later).
- **Never re-`auto_upload` an old job on a live channel.** `POST /api/jobs/{id}/restart`
  re-uploads everything and spends the budget for a second time — see `OPERATIONS.md`.
- **Do not chase the cap with releases.** `PUBLISH_SCHEDULE_GAP_SECONDS` is 3600 (hourly), which is
  far past what Shorts distributes: the growth guidance is **1-3 per day** for aggressive posting
  and **3-5 per week** as the sustainable band, and posting several in one day spreads them thinner
  rather than adding reach. Hourly releases also make the channel look like a repost farm.
- **Check the daily numbers from the DB, not from the log**, and always convert:
  `published_at` is UTC naive, `app.log` is local (EET/UTC+2 in winter, EEST/UTC+3 in summer —
  this mismatch caused one past misdiagnosis).

```bat
sqlite3 ..\storage\app.db "select substr(published_at,1,10) d, count(*) from clips
  where youtube_video_id<>'' group by d order by d desc limit 8;"
```

## 5. State of play, 2026-11-12 (measured here)

Job 109's backlog against a rolling-10 model: 7 uploads inside the window at 01:51 UTC, 8 clips
still queued. Predicted: ~3 successes now (to ~10), then refusals until the oldest successes age
out tonight from **20:52 UTC** onward, one slot roughly every hour from then, finishing the
backlog early on 2026-11-13. Already-scheduled clips are unaffected — their release is YouTube's
job; the ones that have not uploaded get a new hourly slot at handover.

Related: [OPERATIONS.md](OPERATIONS.md), [SHORTS_PACKAGING.md](SHORTS_PACKAGING.md)

## Sources

- [Quota Calculator | YouTube Data API](https://developers.google.com/youtube/data/determine_quota_cost) — 100 `videos.insert`/day, 100 `search.list`/day, 10,000 units, `thumbnails.set` = 50.
- [YouTube Data API Overview / getting started](https://developers.google.com/youtube/data/getting-started) — "a video upload costs 1 unit", invalid requests still cost 1.
- [YouTube API Services form (quota extension)](https://support.google.com/youtube/contact/yt_api_form) — `search.list` and `videos.insert` extensions requested separately.
- [YouTube Daily Upload Limit: When It Resets and How to Raise It](https://savepoint.fm/docs/troubleshooting/fix-youtube-daily-upload-limit) — ~15/day unverified, rolling not midnight, advanced features (6-second video / ID / ~2 months history) raise it.
- [YouTube API Quota Exceeded? Why Uploads Stop at 6 a Day](https://degird.com/blog/youtube-api-quota-exceeded-upload-limit) — the two ceilings, midnight-Pacific quota reset, 1,600-unit arithmetic (now obsolete), no project rotation.
- [YouTube API Pricing 2026: Quota, Upload Limits, Real Costs](https://www.outstand.so/blog/youtube-api-pricing-quota) — `uploadLimitExceeded` as a channel ceiling separate from project quota.
- [YouTube Upload API: Videos & Shorts (2026)](https://postproxy.dev/blog/youtube-upload-api-guide/) — resumable upload + OAuth setup reference.
- [YouTube Uploading Limits 2026](https://postonce.to/blog/youtube-uploading-limits) — per-channel cap unpublished, <10 unverified vs 50+ verified (third-party claim), spread uploads across the calendar.
- [How Many YouTube Shorts Per Day?](https://flowshorts.app/blog/how-many-shorts-per-day) — 1-3/day aggressive, 3-7/week sustainable.
- [YouTube Shorts Algorithm 2026 guide](https://influenceflow.io/resources/youtube-shorts-and-long-form-video-strategy-the-complete-2026-creators-guide-1/) — 3-5/week, don't dump a day's Shorts at once.
- [The YouTube Shorts Algorithm in 2026 — Fastlane](https://www.usefastlane.ai/blog/youtube-shorts-algorithm-2026) — 22-45 s performs best for most niches.
- [Uploading short video. Is there a limit a day? — YouTube Community](https://support.google.com/youtube/thread/374458433/uploading-short-video-is-there-a-limit-a-day?hl=en) — a user blocked after 10 Shorts-length uploads.

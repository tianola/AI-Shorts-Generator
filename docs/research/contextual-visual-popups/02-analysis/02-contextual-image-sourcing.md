# Layer 2: Analysis — Dimension 2: Contextual Image Sourcing & Caching

## 1. Objective & Sourcing Challenge
Modern short-form video requires immediate visual cues. When an automated system searches for contextual visuals, relying solely on a single third-party API introduces latency spikes, rate-limit failures, and copyright/NSFW risks.

To achieve real-time generation speed (< 15 seconds per short), high aesthetic relevance, and 100% uptime, the system employs a **Tiered Multi-Source Waterfall Architecture** backed by an on-disk **SHA-256 Content-Hashed Cache**.

---

## 2. Multi-Provider Waterfall Architecture

```
                      [ Search Query: e.g. "Artificial Intelligence" ]
                                             │
                                             ▼
                     ┌───────────────────────────────────────────────┐
                     │ Tier 0: Local Curated Asset Pack              │
                     │ (Reaction Memes, Icons, Emojis, SVGs/PNGs)    │
                     └───────────────────────┬───────────────────────┘
                                             │
                          ┌──────────────────┴──────────────────┐
                          ▼ Local Exact Match Found?            ▼ No Local Match
                 [ Return Local PNG Path ]                      │
                                                                ▼
                                             ┌───────────────────────────────────────┐
                                             │ Disk Cache Lookup                     │
                                             │ storage/cache/images/{sha256(q)}.png  │
                                             └──────────────────┬────────────────────┘
                                                                │
                                    ┌───────────────────────────┴───────────────────────────┐
                                    ▼ Cache Hit (< 2ms)                                     ▼ Cache Miss
                           [ Return Cached PNG ]                                            │
                                                                                            ▼
                                             ┌───────────────────────────────────────────────────────────┐
                                             │ Tier 1: Pexels Image API                                  │
                                             │ (PEXELS_API_KEY, High-Res Photography, Portrait/Square)   │
                                             └─────────────────────────────┬─────────────────────────────┘
                                                                           │
                                            ┌──────────────────────────────┴──────────────────────────────┐
                                            ▼ Success (200 OK)                                            ▼ 401/429/Empty
                                 [ Cache & Return PNG ]                                                   │
                                                                                                          ▼
                                             ┌───────────────────────────────────────────────────────────┐
                                             │ Tier 2: Wikimedia Commons API                             │
                                             │ (Zero API Key, Public Domain, Named Entities/Logos/Events)│
                                             └─────────────────────────────┬─────────────────────────────┘
                                                                           │
                                            ┌──────────────────────────────┴──────────────────────────────┐
                                            ▼ Success (200 OK)                                            ▼ Empty/Fail
                                 [ Cache & Return PNG ]                                                   │
                                                                                                          ▼
                                             ┌───────────────────────────────────────────────────────────┐
                                             │ Tier 3: Unsplash Image API Fallback                       │
                                             │ (Optional Editorial Photography)                          │
                                             └─────────────────────────────┬─────────────────────────────┘
                                                                           │
                                            ┌──────────────────────────────┴──────────────────────────────┐
                                            ▼ Success                                                     ▼ All Failed
                                 [ Cache & Return PNG ]                                      [ Return Local Generic Fallback ]
```

---

## 3. Comparative Evaluation of Image Providers

| Provider | Authentication | Rate Limits | Ideal Use Cases | Latency (avg) | Failure Mode |
|:---|:---|:---|:---|:---:|:---|
| **Tier 0: Local Pack** | None (Local Filesystem) | Unlimited | Memes, reactions, money, shock, stonks, fire | **< 1 ms** | None (100% reliability) |
| **Tier 1: Pexels API** | API Key (`PEXELS_API_KEY`) | 200 req/hr, 20,000/mo | General stock photos, objects, nature, lifestyle | **180 ms** | Falls back to Wikimedia |
| **Tier 2: Wikimedia Commons** | **None (Zero Key Required)** | Generous public policy | Historical figures, science, flags, logos, landmarks | **220 ms** | Falls back to Unsplash |
| **Tier 3: Unsplash API** | Client-ID Header | 50 req/hr (demo) | Modern aesthetic portraits, architecture, abstract | **260 ms** | Falls back to Local Generic |

### 3.1 Wikimedia Commons: The Zero-Auth Powerhouse
Wikimedia Commons provides a REST endpoint requiring no API keys:
`https://commons.wikimedia.org/w/api.php?action=query&generator=search&gsrsearch={query}&gsrnamespace=6&prop=imageinfo&iiprop=url|size|mime&format=json`
This is particularly effective for specific named entities (e.g., "Nikola Tesla", "Apollo 11", "Statue of Liberty", "Bitcoin logo") where stock photography providers often return irrelevant or generic stock models.

---

## 4. Local Caching Engine & Deduplication

### 4.1 Disk Cache Layout
All downloaded visual assets are normalized, saved as lossless RGBA PNGs, and stored deterministically under:
```
storage/
└── cache/
    └── images/
        ├── a1b2c3d4e5f6...png    # sha256("bitcoin logo")
        ├── f9e8d7c6b5a4...png    # sha256("elon musk")
        └── index.json             # Metadata index (source, timestamp, query)
```

### 4.2 Cache Replacement & Maintenance
- **Key Formulation**: `cache_key = hashlib.sha256(query.strip().lower().encode('utf-8')).hexdigest()`
- **TTL**: 14 days default validity.
- **Max Cache Budget**: 500 MB (automatically evicts oldest unaccessed assets via LRU cleanup upon reaching 90% capacity).
- **Network Resilience**: All HTTP requests utilize `httpx.AsyncClient` with a strict `5.0s` connect/read timeout to avoid hanging the rendering queue.

---

## 5. Image Validation & Normalization Pipeline
Before any remote image enters the FFmpeg compositor, it passes through an automated Pillow normalization pipeline:
1. **Dimension Verification**: Minimum image size `300x300px` (discards low-res thumbnails or tracking pixels).
2. **Aspect Ratio Clamping**: Clamped to a standard square (`1:1`) or landscape card (`4:3` / `16:9`).
3. **Format Normalization**: Converts CMYK, Palette, or RGB formats into clean 32-bit `RGBA` with premultiplied alpha.

---

## SOURCES (LAYER 3 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\03-dossiers\02-image-providers-and-cache-benchmarks.md
 -> Concrete Python implementation of `image_fetcher.py`, HTTP request schemas, and benchmark latency measurements.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\03-animated-popup-spec.md
 -> Pillow card styling pipeline and FFmpeg animation filter specifications.

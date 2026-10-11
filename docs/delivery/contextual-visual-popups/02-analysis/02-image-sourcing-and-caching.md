# 02 Image Sourcing & Caching Analysis

## Architecture Overview
The contextual image engine in `services/image_fetcher.py` provides resilient image retrieval through a tiered waterfall:

```
                  [ Query Input: e.g. "bitcoin logo" ]
                                   │
                           [ Cache Check ]
                     (storage/cache/images/<hash>.png)
                                   │
               ┌───────────────────┴───────────────────┐
            [ Hit ]                                 [ Miss ]
               │                                       │
      Return Cached PNG                        [ Tier 0: Local Assets ]
                                                       │
                                               [ Tier 1: Pexels API ]
                                                       │
                                          [ Tier 2: Wikimedia Commons ]
                                                       │
                                              [ Download & Validate ]
                                                       │
                                                [ Cache to Disk ]
```

## Waterfall Hierarchy

1. **Local Disk Cache**: SHA-256 hash of lowercase query string (`storage/cache/images/{hash}.png`).
2. **Tier 0 (Local Asset Library)**: Direct match in `storage/assets/` or `backend/assets/` for curated meme/reaction packs.
3. **Tier 1 (Pexels Image API)**: High-resolution stock imagery via Pexels API (`https://api.pexels.com/v1/search`), filtered for landscape/square orientation.
4. **Tier 2 (Wikimedia Commons API)**: Keyless public domain search via Wikimedia Action API with bitmap generator (`action=query&generator=search&gsrsearch=filetype:bitmap...`), returning high-quality PNGs, JPEGs, and SVGs.

## Caching Strategy
- Storage directory: `storage/cache/images/`
- Deduplication: Normalized queries are mapped to 256-bit SHA hashes.
- Image sanitization: Downloaded binaries are verified through Pillow, converted to RGB/RGBA, and stored as standardized PNG files.

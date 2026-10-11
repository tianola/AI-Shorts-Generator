# Layer 2: Analysis — Dimension 3: Animated Image Pop-up Specification

## 1. Visual Design & Card Geometry

Vertical video platforms (TikTok, Instagram Reels, YouTube Shorts) feature dense interface overlays at the top and bottom. A popup that covers the center face, title bar, or subtitle zone ruins the clip.

### 1.1 1080x1920 Screen Safe-Zone Geometry

```
 ┌─────────────────────────────────────────────────────────┐  y = 0
 │  [TOP SAFE ZONE] Motion Title Card (y = 36 to 220)       │
 ├─────────────────────────────────────────────────────────┤  y = 250
 │                                                         │
 │  ┌───────────────────────────────────────────────────┐  │  y = 280
 │  │      ★ POPUP SAFE ZONE: UPPER-THIRD CENTER        │  │
 │  │      Target Size: 480px × 360px (or 400×400)      │  │
 │  │      Position: x = (1080 - w)/2 = 300, y = 290    │  │
 │  └───────────────────────────────────────────────────┘  │  y = 650
 │                                                         │
 │                                                         │
 │  [SPEAKER FACE & UPPER TORSO ZONE]                      │
 │  Active Face Track Focus (y = 650 to 1250)              │
 │                                                         │
 ├─────────────────────────────────────────────────────────┤  y = 1250
 │  [CAPTIONS & SUBTITLES ZONE]                            │
 │  Karaoke / Hormozi Subtitles (y = 1300 to 1650)         │
 ├─────────────────────────────────────────────────────────┤  y = 1700
 │  [BOTTOM PLATFORM UI ZONE] Likes / Comments / Nav Bar    │
 └─────────────────────────────────────────────────────────┘  y = 1920
```

### 1.2 Popup Card Canvas Specifications
To achieve modern studio quality, raw images are not directly overlaid onto video. Instead, they are wrapped into a polished **Visual Card**:
- **Outer Canvas Size**: 560 × 440 px (incorporates drop-shadow padding).
- **Core Card Size**: 480 × 360 px (for 4:3 landscape) or 400 × 400 px (for 1:1 square).
- **Corner Radius**: 24 px antialiased rounding.
- **Border**: 4 px crisp solid border (`#FFFFFF` with 95% opacity or theme accent `#00F0FF`).
- **Drop Shadow**: Gaussian blur (sigma = 14 px), vertical offset `dy = +10 px`, color `RGBA(0, 0, 0, 140)`.

---

## 2. Animation Physics: The Spring Scale-Pop

A standard linear scale or cross-dissolve looks amateurish and robotic. Viral high-retention content utilizes an **Elastic Spring Overshoot**:
1. **Anticipation & Growth (`t = 0.00s` to `0.18s`)**: Scale rapidly expands from `0.0x` to `1.12x` (overshoot).
2. **Elastic Settle (`t = 0.18s` to `0.28s`)**: Elastic recovery contracts from `1.12x` down to `1.00x`.
3. **Steady State (`t = 0.28s` to `T_DURATION - 0.20s`)**: Subtle breathing oscillation (±0.8% scale) to keep the asset dynamic.
4. **Exit Dissolve / Shrink (`t = T_DURATION - 0.20s` to `T_DURATION`)**: Smooth fade-out (`alpha -> 0`) combined with a slight shrink (`1.0x -> 0.92x`).

### 2.1 Mathematical Formula for FFmpeg `eval=frame`

Let $t_r = t - t_{\text{start}}$ be the relative time in seconds:

$$\text{Scale}(t_r) = \begin{cases} 
0.85 + 0.27 \cdot \sin\left(\frac{t_r}{0.18} \cdot \frac{\pi}{2}\right) & \text{if } 0 \le t_r < 0.18 \quad (\text{Overshoot to } 1.12) \\
1.12 - 0.12 \cdot \left(\frac{t_r - 0.18}{0.10}\right) & \text{if } 0.18 \le t_r < 0.28 \quad (\text{Settle to } 1.00) \\
1.00 + 0.008 \cdot \sin(2\pi \cdot t_r) & \text{if } 0.28 \le t_r < T_d - 0.20 \quad (\text{Breathing}) \\
1.00 - 0.08 \cdot \left(\frac{t_r - (T_d - 0.20)}{0.20}\right) & \text{if } t_r \ge T_d - 0.20 \quad (\text{Exit})
\end{cases}$$

---

## 3. Production FFmpeg Filtergraph Construction

### 3.1 Direct Filter String Template (Single-Pass Compatible)

When integrating into FFmpeg's `filter_complex`, the card PNG is fed as an input stream `[idx:v]` with `-loop 1`:

```
[1:v]format=rgba,
scale=w='iw*(0.85+0.27*clip((t-T_START)/0.18,0,1)-0.12*clip((t-T_START-0.18)/0.10,0,1))':
      h='ih*(0.85+0.27*clip((t-T_START)/0.18,0,1)-0.12*clip((t-T_START-0.18)/0.10,0,1))':
      eval=frame,
fade=t=in:st=T_START:d=0.18:alpha=1,
fade=t=out:st=T_END-0.20:d=0.20:alpha=1[popup_anim];

[cur_v][popup_anim]overlay=x='(W-w)/2':y='290+15*(1-clip((t-T_START)/0.18,0,1))':
                           enable='between(t,T_START,T_END)':shortest=1[vout]
```

### 3.2 Slide-In From Right Variant (For Side Cards / Entity Badges)
For widescreen split layouts or when upper center is occupied:
- **X-Coordinate Expression**: `x='W-w-50+120*(1-clip((t-T_START)/0.22,0,1))'` (slides in smoothly from the right margin, settles 50px from edge).
- **Y-Coordinate Expression**: `y=320` (fixed upper-right position).

---

## SOURCES (LAYER 3 NAVIGATION)
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\03-dossiers\03-ffmpeg-popup-filtergraphs.md
 -> Complete tested FFmpeg commands, parameter matrices, and standalone execution verification scripts.
E:\01-AI-PROJECTS\ai-shorts-generator\docs\research\contextual-visual-popups\02-analysis\04-system-api-architecture.md
 -> Integration of filtergraph assembly into `render.py` (`build_single_pass_filter`).

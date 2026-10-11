# 03 Card Styling & FFmpeg Filtergraph Analysis

## 1. Card Layout & Visual Styling (Pillow)
Raw images retrieved from external APIs have inconsistent dimensions and hard edges. `services/visual_popup.py` applies styling before overlaying onto the video:

- **Aspect Ratio & Resize**: Images are scaled and cropped to `480x340` (or configured dimensions) using Lanczos antialiasing (`ImageOps.fit`).
- **Rounded Corners**: A 24px radius mask softens corners.
- **Border**: A 4px white translucent border (`RGBA(255, 255, 255, 240)`) adds contrast against busy backgrounds.
- **Soft Drop Shadow**: Rendered onto an expanded transparent canvas with a 14px Gaussian blur radius and 10px downward offset (`RGBA(0, 0, 0, 150)`).

## 2. Dynamic Spring Animation (FFmpeg)
Rather than a static cut, popups enter with a smooth spring overshoot animation evaluated dynamically per frame in FFmpeg.

### Scale Curve
$$\text{scale}(t) = \text{iw} \cdot \left(0.85 + 0.27 \cdot \text{clip}\left(\frac{t - t_{\text{start}}}{0.15}, 0, 1\right) - 0.12 \cdot \text{clip}\left(\frac{t - t_{\text{settle}}}{0.08}, 0, 1\right)\right)$$
- At $t = t_{\text{start}}$: Scale = `0.85`
- At $t = t_{\text{start}} + 0.15$: Scale peaks at `1.12` (overshoot)
- At $t = t_{\text{start}} + 0.23$: Scale settles to `1.00`

### Positional Geometry
- **Upper Third** (Default): Centered horizontally at $x = (W-w)/2$, dropping gently from $y = 315$ to $y = 290$. Avoids colliding with bottom captions ($y \approx 1250..1650$) and top motion titles ($y \approx 160..240$).
- **Upper Right**: Positioned at $x = W - w - 50$, $y = 320$.

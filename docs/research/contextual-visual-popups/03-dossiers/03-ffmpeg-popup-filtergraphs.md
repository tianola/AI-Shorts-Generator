# Layer 3: Dossier — 03: FFmpeg Filtergraph Mathematics and Assembly

## 1. Filtergraph Animation Mathematics

### 1.1 Spring Overshoot Scale Curve (`eval=frame`)
The scale animation utilizes an analytical approximation of an underdamped spring response over duration $T_{\text{in}} = 0.20s$:

$$\text{scale}(t) = 0.85 + 0.27 \cdot \text{clip}\left(\frac{t - T_{\text{start}}}{0.15}, 0, 1\right) - 0.12 \cdot \text{clip}\left(\frac{t - T_{\text{start}} - 0.15}{0.08}, 0, 1\right)$$

In FFmpeg `scale` filter syntax (escaped for filter_complex):
```text
scale=w='iw*(0.85+0.27*clip((t-T_START)/0.15,0,1)-0.12*clip((t-T_START-0.15)/0.08,0,1))':h='ih*(0.85+0.27*clip((t-T_START)/0.15,0,1)-0.12*clip((t-T_START-0.15)/0.08,0,1))':eval=frame
```

### 1.2 Smooth Entry Slide-Down (Y-Offset)
To make the card pop in with physical weight, it begins 25 pixels lower and rises to its resting position at `y = 290`:
```text
y='290+25*(1-clip((t-T_START)/0.18,0,1))'
```

### 1.3 Alpha Fade In / Fade Out
```text
fade=t=in:st=T_START:d=0.15:alpha=1,fade=t=out:st=T_END-0.18:d=0.18:alpha=1
```

---

## 2. Standalone FFmpeg Test CLI Command

The following command can be used to verify popup rendering on any 1080x1920 base video:

```bash
ffmpeg -y \
  -i base_vertical.mp4 \
  -loop 1 -i popup_card.png \
  -filter_complex "\
    [1:v]format=rgba,\
         scale=w='iw*(0.85+0.27*clip((t-3.0)/0.15,0,1)-0.12*clip((t-3.15)/0.08,0,1))':\
               h='ih*(0.85+0.27*clip((t-3.0)/0.15,0,1)-0.12*clip((t-3.15)/0.08,0,1))':\
               eval=frame,\
         fade=t=in:st=3.0:d=0.15:alpha=1,\
         fade=t=out:st=4.82:d=0.18:alpha=1[popup_anim];\
    [0:v][popup_anim]overlay=x='(W-w)/2':y='290+25*(1-clip((t-3.0)/0.18,0,1))':\
                             enable='between(t,3.0,5.0)':shortest=1[vout]" \
  -map "[vout]" -map "0:a?" \
  -c:v libx264 -preset veryfast -crf 20 -c:a copy \
  test_output_with_popup.mp4
```

---

## 3. Python Filter Assembly for `services/render.py`

```python
def build_popup_filtergraph_fragment(
    input_idx: int,
    start_time: float,
    duration: float = 2.0,
    position: str = "upper_third"
) -> tuple[str, str]:
    """
    Constructs the FFmpeg filter strings for a popup overlay node.
    
    Returns:
        (filter_definition, overlay_definition)
    """
    t_start = round(float(start_time), 3)
    t_dur = round(float(duration), 3)
    t_end = round(t_start + t_dur, 3)
    t_settle = round(t_start + 0.15, 3)
    t_fadeout = round(t_end - 0.18, 3)

    # Scale and fade preparation
    scale_expr = (
        f"iw*(0.85+0.27*clip((t-{t_start})/0.15\\,0\\,1)-0.12*clip((t-{t_settle})/0.08\\,0\\,1))"
    )
    
    anim_label = f"popup_anim_{input_idx}"
    prep_filter = (
        f"[{input_idx}:v]format=rgba,"
        f"scale=w='{scale_expr}':h='{scale_expr}':eval=frame,"
        f"fade=t=in:st={t_start}:d=0.15:alpha=1,"
        f"fade=t=out:st={t_fadeout}:d=0.18:alpha=1[{anim_label}]"
    )

    if position == "upper_right":
        x_expr = f"W-w-50+80*(1-clip((t-{t_start})/0.18\\,0\\,1))"
        y_expr = "320"
    else:  # upper_third (center)
        x_expr = "(W-w)/2"
        y_expr = f"290+25*(1-clip((t-{t_start})/0.18\\,0\\,1))"

    overlay_expr = (
        f"overlay=x='{x_expr}':y='{y_expr}':"
        f"enable='between(t\\,{t_start}\\,{t_end})':shortest=1"
    )

    return prep_filter, anim_label, overlay_expr
```

---

## 4. Multi-Popup Sequencing in Single Pass

When a clip contains 2 popups:
1. `popup_1.png` is assigned `extra_inputs` index `N` (`-loop 1`).
2. `popup_2.png` is assigned `extra_inputs` index `N+1` (`-loop 1`).
3. Filtergraph chains linearly: `[cur_v][popup_anim_1]overlay=...[vpop1]; [vpop1][popup_anim_2]overlay=...[vpop2]`.
4. The output label `[vpop2]` feeds directly into subsequent stages (e.g. motion title or final output).

---

## SOURCES
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\services\render.py` -> `build_single_pass_filter` integration.
- `E:\01-AI-PROJECTS\ai-shorts-generator\backend\tests\test_single_pass_unified.py` -> Test harness for filter verification.

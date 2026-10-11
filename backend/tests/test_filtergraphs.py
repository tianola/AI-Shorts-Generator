"""
Structural checks on the ffmpeg filtergraphs the services build.

These run without ffmpeg installed, and without pytest (see __main__), because
a malformed graph only shows up as a render failure minutes into a job.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TOKEN_RE = re.compile(r"\[[A-Za-z0-9_:]+\]|[^[\]]+")


def _is_label(token: str) -> bool:
    return token.startswith("[") and token.endswith("]") and len(token) > 2


def _validate_graph(fc: str) -> list:
    """Return a list of human-readable graph errors (empty means valid).

    Models the subset of ffmpeg syntax the services emit: within a ';' chain,
    bracketed labels appear only as the chain's inputs (leading) or outputs
    (trailing).
    """
    errors = []
    if fc.count("(") != fc.count(")"):
        errors.append(f"unbalanced parentheses ({fc.count('(')} open / {fc.count(')')} close)")

    defined = {}
    consumed = {}

    for chain in fc.split(";"):
        chain = chain.strip()
        if not chain:
            errors.append("empty filterchain")
            continue
        toks = [t for t in TOKEN_RE.findall(chain) if t.strip()]
        i = 0
        inputs = []
        while i < len(toks) and _is_label(toks[i]):
            inputs.append(toks[i][1:-1])
            i += 1
        j = len(toks)
        outputs = []
        while j > i and _is_label(toks[j - 1]):
            outputs.insert(0, toks[j - 1][1:-1])
            j -= 1
        body = "".join(toks[i:j]).strip().strip(",")
        if not re.match(r"^[A-Za-z0-9_]+", body):
            errors.append(f"chain has no filter name: {chain[:60]!r}")
        if not outputs:
            errors.append(f"chain does not label its output: {chain[:60]!r}")

        for lab in outputs:
            if lab in defined:
                errors.append(f"label [{lab}] is produced by two chains")
            defined[lab] = True
        for lab in inputs:
            consumed[lab] = consumed.get(lab, 0) + 1

    for lab, count in consumed.items():
        if count > 1:
            errors.append(f"pad [{lab}] is consumed {count} times; use split to reuse a pad")
        if lab not in defined and ":" not in lab:
            errors.append(f"[{lab}] is read before any chain produces it")
    return errors


def _assert_valid(fc: str, ctx: str):
    errors = _validate_graph(fc)
    if errors:
        raise AssertionError(f"{ctx}: {errors}\ngraph: {fc}")


def test_piecewise_expr_is_balanced():
    from services.vertical import _piecewise_expr
    points = [(0.0, 100, 50, 300, 500), (0.5, 120, 55, 300, 500), (1.0, 140, 60, 300, 500)]
    for idx in (1, 2):
        expr = _piecewise_expr(points, idx)
        assert expr.count("(") == expr.count(")"), expr
        # if()/clip() take 3 args (2 commas each), lt() takes 2 (1 comma)
        expected = (2 * expr.count("if(") + 2 * expr.count("clip(")
                    + expr.count("lt("))
        assert expr.count(",") == expected, expr
        assert "\\," not in expr, expr
    assert _piecewise_expr([(0.0, 7, 9, 2, 2)], 1) == "7"


def test_graph_validator_rejects_known_bad_shapes():
    assert _validate_graph("[0:v]crop=10:x='if(lt(t,1),2,3',y='0'[a];[a]null[o]")
    assert _validate_graph("[0:v]split=2[a][a];[a]null[b];[a]null[o]")
    assert _validate_graph("[0:v]scale=100:100")  # unlabeled output
    assert not _validate_graph("[0:v]split=2[a][b];[a]null[c];[b][c]overlay[o]")


def test_popup_fragment_has_no_stray_backslashes():
    from services.visual_popup import build_popup_filtergraph_fragment
    prep, anim, overlay = build_popup_filtergraph_fragment(1, 2.5, 2.0, "upper_third")
    graph = f"[0:v]null[base];[base][{anim}]{overlay}[o]"
    assert "\\," not in prep, prep
    _assert_valid(prep + ";" + graph, "popup")


def test_timed_crop_graph():
    from services.vertical import _build_timed_crop
    traj = [(0.0, 100, 50, 300, 500), (0.5, 120, 55, 300, 500), (1.0, 140, 60, 300, 500)]
    frag = _build_timed_crop(traj)
    assert frag.startswith("crop=300:500:"), frag
    _assert_valid(f"[0:v]{frag},scale=1080:1920[vsingle]", "timed crop")


def test_blur_overlay_clamps_and_splits():
    from services.focus_tracking import _build_blur_box_overlay
    traj = [(0.0, 1900, 1000, 400, 300), (1.0, 520, 310, 200, 100)]
    frag = _build_blur_box_overlay(traj, 1920, 1080)
    assert "split=2" in frag, frag
    _assert_valid(frag, "blur overlay")


def test_blurred_fit_graph():
    from services.focus_tracking import build_blurred_fit_filter
    _assert_valid(build_blurred_fit_filter(1920, 1080), "fit_blur")


def test_split_screen_graph():
    from services.focus_tracking import build_split_screen_filter
    _assert_valid(build_split_screen_filter((100, 80, 300, 600), (900, 80, 300, 600), 1920, 1080),
                  "split_screen")


def test_single_pass_graph_with_all_stages():
    from services.render import build_single_pass_filter
    from services.focus_tracking import build_blurred_fit_filter, _build_blur_box_overlay
    blur = _build_blur_box_overlay([(0.0, 500, 300, 200, 100), (1.0, 520, 310, 200, 100)], 1920, 1080)
    _, fc, vmap, amap = build_single_pass_filter(
        build_blurred_fit_filter(1920, 1080).replace("[outv]", "[vsingle]"),
        captions_path="C:/jobs/1/captions.ass",
        title_png="C:/jobs/1/title.png",
        sfx_files=["C:/jobs/1/whoosh.mp3"],
        sfx_timestamps=[0.3],
        has_audio=True,
        smart_blur_frag=blur,
    )
    _assert_valid(fc, "single pass with blur + fit_blur + captions + title + sfx")
    assert vmap == "[vout]", vmap
    assert "normalize=0" in fc, fc


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:
                failed += 1
                print(f"FAIL {name}: {e}")
    sys.exit(1 if failed else 0)

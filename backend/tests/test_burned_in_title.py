"""The headline burned into the frame must be the final title, not a draft.

Single-pass mode composites the title PNG *inside* the render, so whatever text
is passed there is what the viewer reads forever. `worker.py` generated its
metadata after the render, which left job 105 with "VIRAL MOMENT 1" burned into
every clip while the database and YouTube carried the real title
("The Strategy Only Top 1% of Learners Use"). The overlay pass that would have
corrected it is skipped in single-pass mode.
"""
import re
import inspect

import worker


def _clip_processing_source() -> str:
    return inspect.getsource(worker.process_clip)


def test_metadata_is_generated_before_the_single_pass_render():
    src = _clip_processing_source()
    metadata_at = src.index("await generate_metadata(")
    render_at = src.index("_rsp(")
    assert metadata_at < render_at, (
        "generate_metadata must run before render_single_pass: the title it "
        "returns is the text composited into the frame in single-pass mode"
    )


def test_the_burned_in_title_comes_from_the_generated_metadata():
    src = _clip_processing_source()
    render_call = src[src.index("_rsp("):src.index("source_channel=source_channel,") + 40]
    assert re.search(r"title_text=(_title|final_title)\b", render_call), render_call
    assert '"Viral Moment"' not in src, (
        "no publish or render path may substitute a placeholder headline"
    )

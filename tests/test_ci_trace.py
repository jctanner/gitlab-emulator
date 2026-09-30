"""Rendering of runner job traces: a plain log with colours and no control bytes."""

import re

from app.web.ci_trace import render_trace

ESC = "\x1b"


def _trace(*lines: str) -> str:
    return "\n".join(lines) + "\n"


def _text(page: str) -> str:
    """The page as a reader sees it: tags removed, entities decoded."""
    import html

    return html.unescape(re.sub(r"<[^>]+>", "", page))


# Shapes taken from a real gitlab-runner trace with timestamps enabled.
RUNNER_TRACE = _trace(
    f"2026-09-30T19:33:41.994410Z 00O {ESC}[0KRunning with gitlab-runner 19.1.1{ESC}[0;m",
    "2026-09-30T19:33:41.994614Z 00O section_start:1790796821:prepare_executor",
    f"{ESC}[0K",
    f'2026-09-30T19:33:41.994620Z 00O+{ESC}[0K{ESC}[36;1mPreparing the "kubernetes" executor{ESC}[0;m{ESC}[0;m',
    f"2026-09-30T19:33:41.994661Z 00O {ESC}[0KUsing Kubernetes namespace: gitlab-runner{ESC}[0;m",
    "2026-09-30T19:33:42.994700Z 00O section_end:1790796825:prepare_executor",
    f"{ESC}[0K",
    f"2026-09-30T19:33:46.602799Z 01O {ESC}[32;1m$ bash run.sh{ESC}[0;m",
    f"2026-09-30T19:35:18.699998Z 00E {ESC}[31;1mERROR: Job failed{ESC}[0;m",
)


def test_no_control_sequences_or_section_markers_reach_the_page():
    page = str(render_trace(RUNNER_TRACE))
    assert ESC not in page
    assert "section_start" not in page
    assert "section_end" not in page
    assert "[0K" not in page
    assert "[0;m" not in page


def test_output_is_a_plain_log_with_nothing_added():
    page = str(render_trace(RUNNER_TRACE))
    for added in ("<details", "<summary", "<div", "ci-ln", "ci-duration", "title="):
        assert added not in page


def test_timestamp_and_stream_prefix_are_kept_as_written():
    lines = _text(str(render_trace(RUNNER_TRACE))).split("\n")
    assert lines[0] == "2026-09-30T19:33:41.994410Z 00O Running with gitlab-runner 19.1.1"
    # The marker line and its "+" continuation are one line, keeping the
    # marker line's prefix.
    assert lines[1] == '2026-09-30T19:33:41.994614Z 00O Preparing the "kubernetes" executor'
    assert lines[-2].startswith("2026-09-30T19:33:46.602799Z 01O $ bash run.sh")
    assert lines[-1].startswith("2026-09-30T19:35:18.699998Z 00E ERROR: Job failed")


def test_marker_only_lines_are_dropped_but_blank_output_lines_are_kept():
    text = _text(
        str(
            render_trace(
                _trace(
                    "2026-09-30T19:33:41.1Z 00O one",
                    "2026-09-30T19:33:41.2Z 00O section_end:5:x",
                    f"{ESC}[0K",
                    "2026-09-30T19:33:41.3Z 01O ",
                    "2026-09-30T19:33:41.4Z 00O two",
                )
            )
        )
    )
    assert text.split("\n") == [
        "2026-09-30T19:33:41.1Z 00O one",
        "2026-09-30T19:33:41.3Z 01O ",
        "2026-09-30T19:33:41.4Z 00O two",
    ]


def test_colours_are_kept_as_spans():
    page = str(render_trace(RUNNER_TRACE))
    assert 'class="ansi-fg-6 ansi-bold">Preparing the "kubernetes" executor</span>' in page
    assert 'class="ansi-fg-2 ansi-bold">$ bash run.sh</span>' in page
    assert 'class="ansi-fg-1 ansi-bold">ERROR: Job failed</span>' in page


def test_output_is_html_escaped():
    page = str(render_trace("2026-09-30T19:33:41.1Z 00O <script>alert(1)</script> & done\n"))
    assert "<script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt; &amp; done" in page


def test_plain_untimestamped_trace_keeps_every_line_including_blank_ones():
    assert str(render_trace("one\n\ntwo\n")) == "one\n\ntwo"


def test_carriage_return_keeps_the_last_redraw():
    text = _text(str(render_trace("2026-09-30T19:33:41.1Z 00O 10%\r50%\r100%\n")))
    assert text == "2026-09-30T19:33:41.1Z 00O 100%"


def test_256_and_truecolour_codes():
    page = str(
        render_trace(f"{ESC}[38;5;196mred{ESC}[0m {ESC}[38;2;1;2;3mrgb{ESC}[0m {ESC}[48;5;21mbg{ESC}[0m\n")
    )
    assert 'style="color:#ff0000"' in page
    assert 'style="color:#010203"' in page
    assert "background-color:#0000ff" in page


def test_section_end_followed_directly_by_the_next_section_start():
    # Exact shape from a real trace: end marker, bare ESC[0K, then the next
    # start marker arriving as a "+" continuation of the end marker's line.
    text = _text(
        str(
            render_trace(
                _trace(
                    "2026-09-30T19:33:41.0Z 00O section_start:10:one",
                    f"{ESC}[0K",
                    f"2026-09-30T19:33:41.1Z 00O+{ESC}[0K{ESC}[36;1mFirst{ESC}[0;m",
                    "2026-09-30T19:33:41.2Z 01O body of one",
                    "2026-09-30T19:33:42.0Z 00O section_end:12:one",
                    f"{ESC}[0K",
                    "2026-09-30T19:33:42.1Z 00O+section_start:12:two",
                    f"{ESC}[0K",
                    f"2026-09-30T19:33:42.2Z 00O+{ESC}[0K{ESC}[36;1mSecond{ESC}[0;m",
                    "2026-09-30T19:33:43.0Z 00O section_end:15:two",
                    f"{ESC}[0K",
                )
            )
        )
    )
    assert "section_" not in text
    assert text.split("\n") == [
        "2026-09-30T19:33:41.0Z 00O First",
        "2026-09-30T19:33:41.2Z 01O body of one",
        "2026-09-30T19:33:42.0Z 00O Second",
    ]


def test_marker_options_are_removed():
    text = _text(str(render_trace("section_start:100:build[collapsed=true]\rBuilding\nstill going\n")))
    assert text == "Building\nstill going"


def test_empty_trace():
    assert str(render_trace("")) == ""
    assert str(render_trace(None)) == ""

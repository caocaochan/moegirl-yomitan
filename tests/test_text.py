from moegirl_yomitan.text import normalize_whitespace, trim_summary


def test_normalize_whitespace_collapses_runs() -> None:
    assert normalize_whitespace("  A \n\n B\tC  ") == "A B C"


def test_trim_summary_keeps_short_text() -> None:
    assert trim_summary("简短摘要。", 20) == "简短摘要。"


def test_trim_summary_prefers_sentence_boundary() -> None:
    text = "第一句很重要。第二句也重要。第三句会被截断，因为它太长了。"
    assert trim_summary(text, 16) == "第一句很重要。第二句也重要。"


def test_trim_summary_falls_back_to_ellipsis() -> None:
    text = "abcdefghijklmnopqrstuvwxyz"
    assert trim_summary(text, 10).endswith("…")

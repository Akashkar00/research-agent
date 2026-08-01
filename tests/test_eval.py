from app.eval import _parse_score, citation_support_rate


def test_parse_score_valid():
    assert _parse_score("SCORE: 8/10\nGood report.") == 0.8


def test_parse_score_garbage_returns_none_not_a_fabricated_score():
    # The old behaviour returned 0.5 on parse failure, making a broken judge
    # indistinguishable from a real mediocre score.
    assert _parse_score("The model rambled without following instructions.") is None


def test_parse_score_case_insensitive():
    assert _parse_score("score: 6/10 fine") == 0.6


def test_citation_support_rate_all_cited():
    # Convention: [n] sits before the sentence's closing punctuation (matches the
    # instruction given to the writer/search agents).
    report = (
        "This is a claim that is reasonably long [1]. "
        "Here is another claim with a citation attached to it [2]. "
    )
    assert citation_support_rate(report) == 1.0


def test_citation_support_rate_partial():
    report = (
        "This claim has a citation attached right here [1]. "
        "This other claim has no citation attached at all in this sentence. "
    )
    rate = citation_support_rate(report)
    assert 0.0 < rate < 1.0


def test_citation_support_rate_ignores_references_section():
    report = (
        "One cited claim right here in the body text [1]. "
        "## References\n"
        "[1] Some Source — https://example.com/no-citation-marker-here\n"
    )
    # The references list itself shouldn't be counted as an uncited "claim"
    assert citation_support_rate(report) == 1.0


def test_citation_support_rate_empty_report():
    assert citation_support_rate("") == 0.0

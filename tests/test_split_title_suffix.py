from scripts import content_finalizer as cf


def _para(n):
    return "测" * n


def test_resplit_does_not_stack_part_suffix():
    lo = cf.MIN_CHAPTER_CHARS
    body = _para(lo + 10) + "\n\n" + _para(lo + 10)
    block = {"title": "FDA 争议和血压边界：上篇", "body": body}
    mapping = {"title": block["title"], "unit_ids": ["U0009", "U0010"],
               "claim_ids": ["U0009-C01", "U0010-C01"]}
    out = cf._safe_split_chapter(block, mapping)
    assert out is not None
    titles = [b["title"] for b in out[0]]
    assert titles == ["FDA 争议和血压边界：上篇", "FDA 争议和血压边界：下篇"]

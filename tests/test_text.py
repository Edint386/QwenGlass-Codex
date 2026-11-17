from glasscodex.cleanup import clean, fold
from glasscodex.text import paginate


def test_secrets_are_redacted_and_code_is_folded():
    text = "token: sk-abcdefghijklmnopqrstuvwxyz\n\n```python\nprint('x')\n```\n结束"
    result = clean(text)
    assert "sk-" not in result
    assert "〔已隐藏〕" in result
    assert "〔代码 1 行〕" in result
    assert "结束" in result


def test_pagination_keeps_every_line():
    text = "\n".join(f"第 {i} 行" for i in range(30))
    pages = paginate(text, 386, 18)
    assert len(pages) > 1
    assert "第 0 行" in pages[0]
    assert "第 29 行" in pages[-1]


def test_fold_handles_unclosed_fence():
    assert "〔代码 2 行〕" in fold("```\na\nb")

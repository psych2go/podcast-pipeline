from scripts.validator import normalize_tts_digits


def test_hyphenated_and_parenthesized_identifiers_keep_digits():
    assert normalize_tts_digits("使用 GLP-1 时") == "使用 GLP-1 时"
    assert normalize_tts_digits("COVID-19 期间") == "COVID-19 期间"
    assert normalize_tts_digits("FDA 510(k) 许可") == "FDA 510(k) 许可"


def test_plain_numbers_still_spelled():
    assert normalize_tts_digits("约 50% 脂肪") == "约 百分之五十 脂肪"
    assert normalize_tts_digits("1.5 倍") == "一点五 倍"
    assert normalize_tts_digits("2024 年") == "二零二四 年"

from m2help import STATIC, check_script, ships


def test_refresh_script():
    check_script("refresh.js")
    assert ships("static/refresh.js")
    assert "https?:" in (STATIC / "refresh.js").read_text()      # links from paper data must be http(s)

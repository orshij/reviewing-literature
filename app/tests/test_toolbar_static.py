from m2help import check_script, ships


def test_toolbar_script():
    check_script("toolbar.js")
    assert ships("static/toolbar.js")

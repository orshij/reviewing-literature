from m2help import check_script, ships


def test_chat_script():
    check_script("chat.js")
    assert ships("static/chat.js")

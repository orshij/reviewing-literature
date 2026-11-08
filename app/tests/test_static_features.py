import shutil
import subprocess
from fnmatch import fnmatch
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1]
STATIC = APP / "src" / "litrev_app" / "static"


@pytest.mark.skipif(not shutil.which("node"), reason="needs node for a syntax check")
@pytest.mark.parametrize("name", ["doctor.js", "publish.js"])
def test_feature_scripts_parse(name):
    subprocess.run(["node", "--check", str(STATIC / name)], check=True)


def test_feature_scripts_follow_the_contract_and_the_csp():
    d, p = (STATIC / "doctor.js").read_text(), (STATIC / "publish.js").read_text()
    assert "LR.pages.doctor" in d and "/api/doctor" in d
    assert "LR.features.publish = " in p and "LR.publish = " in p and "LR.reviewTabs.push(" in p
    assert "/publish" in p and "waiting for your approval in the terminal window" in p.lower()
    for js, css in ((d, "/static/doctor.css"), (p, "/static/publish.css")):
        assert css in js and (STATIC / css.split("/")[-1]).exists()
        assert "innerHTML" not in js and '"style"' not in js and ".style." not in js     # style-src 'self'


def test_index_loads_the_feature_scripts_after_app_js():
    html = (STATIC / "index.html").read_text()
    a, d, p = html.find("app.js"), html.find("/static/doctor.js"), html.find("/static/publish.js")
    end = html.find("<!-- /feature scripts -->")
    assert "<!-- feature scripts -->" in html and -1 < a < d < end and -1 < a < p < end


def test_review_page_publish_button_opens_the_dialog():
    pages = [STATIC / n for n in ("review.js", "app.js") if (STATIC / n).exists()]
    assert any("LR.features.publish(" in f.read_text() for f in pages)


def test_package_ships_the_static_files_and_the_cli():
    tomllib = pytest.importorskip("tomllib")
    cfg = tomllib.loads((APP / "pyproject.toml").read_text())
    assert cfg["project"]["scripts"].get("litrev-app") == "litrev_app.cli:main"
    pats = cfg["tool"]["setuptools"]["package-data"]["litrev_app"]
    for f in ("static/index.html", "static/doctor.js", "static/doctor.css", "static/publish.js", "briefs/step.txt"):
        assert any(fnmatch(f, pat) for pat in pats), f


def test_m2_scripts_style_through_a_stylesheet_file():
    """Live (user screenshot): the header read "Claude usage5 h22%week27%null" with no styling. The app's CSP
    (style-src 'self') blocks the <style> elements the M2 scripts injected; and replaceChildren() turns a null
    argument into the text "null"."""
    import re
    from m2help import STATIC, ships
    css = (STATIC / "m2.css").read_text()
    for name, cls in (("toolbar.js", ".m2-toolbar"), ("chat.js", ".m2-chat-list"), ("refresh.js", ".m2-refresh")):
        src = (STATIC / name).read_text()
        assert 'createElement("style")' not in src and "/static/m2.css" in src, name
        assert cls in css, cls
        assert not re.search(r"replaceChildren\((?:[^;]|\n)*?:\s*null\)", src), f"{name} passes null to replaceChildren"
    assert ships("static/m2.css")


def test_chat_and_new_papers_reload_when_the_browser_tab_is_shown_again():
    """UI check: a hidden tab closes its live streams (M1b), and chat events are live-only, so a chat answered while
    the tab was hidden stayed stale with Send disabled until a reload. review.js reloads on 'visible'; these must too."""
    from m2help import STATIC
    for name in ("chat.js", "refresh.js"):
        src = (STATIC / name).read_text()
        assert 'LR.on("visible", () => load())' in src and "offVisible()" in src, name


def test_new_papers_can_check_again_after_the_steps_ran():
    """UI check: after a refresh's steps 4-7 finished, the tab stayed at "Steps 4-7 are queued" with no Find button
    (shown only for idle/done/failed), so the review could never be refreshed again."""
    from m2help import STATIC
    src = (STATIC / "refresh.js").read_text()
    assert "ran = stage === \"queued\"" in src and "Steps 4–7 ran for" in src
    assert '["idle", "done", "failed"].includes(stage) || ran' in src

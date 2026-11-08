import pytest


@pytest.fixture(autouse=True)
def _no_real_voice_venv(tmp_path, monkeypatch):
    """The Doctor's voice probe runs the voice venv's python. A test that builds its Inputs from os.environ and the
    real home would probe this Mac's ~/.local/share/litrev/tts; point it at a folder that does not exist."""
    monkeypatch.setenv("LITREV_VOICE_HOME", str(tmp_path / "no-voice-venv"))

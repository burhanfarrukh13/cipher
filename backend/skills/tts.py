"""
TTS skill — intentionally empty.

Cipher speaks via the browser's built-in speechSynthesis (the frontend
just calls window.speechSynthesis.speak directly). ElevenLabs was removed
because it added latency and a monthly cost for a feature the OS already
provides for free. If you ever want a cloud/custom voice back, add a new
endpoint here and wire it into frontend/index.html.
"""

def is_configured() -> bool:
    return False


def synthesize(*args, **kwargs) -> dict:
    return {"error": "Cloud TTS was removed — Cipher uses the browser's built-in voice."}

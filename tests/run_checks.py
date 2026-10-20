"""Run the suite and expose actionable failures in GitHub's public annotations."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    # Windows redirected consoles may otherwise reject Russian assertion text.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if os.getenv("GITHUB_ACTIONS") == "true":
        for test, detail in result.errors + result.failures:
            token = os.getenv("BOT_TOKEN")
            if token:
                detail = detail.replace(token, "[TOKEN HIDDEN]")
            message = (str(test) + "\n" + detail)[-7000:]
            message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
            print("::error title=Test failure::" + message, flush=True)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())

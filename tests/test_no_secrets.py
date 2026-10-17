"""Fails if anything key-shaped is committed. Keys must live only in environment variables."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".venv", ".git", "__pycache__", "node_modules"}
PATTERNS = {
    "zai-style key (id.secret)": re.compile(r"\b[0-9a-f]{32}\.[A-Za-z0-9]{12,}\b"),
    "sk- style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "bearer token literal": re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{24,}"),
    "aws access key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "hard-coded api key assignment": re.compile(r"""(?i)api[_-]?key\s*[:=]\s*["'][A-Za-z0-9._-]{16,}["']"""),
}


class NoSecrets(unittest.TestCase):
    def test_repository_contains_no_secrets(self):
        hits = []
        for f in ROOT.rglob("*"):
            if not f.is_file() or SKIP_DIRS & set(f.parts) or f.name == "test_no_secrets.py" or f.stat().st_size > 2_000_000:
                continue
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for name, rx in PATTERNS.items():
                if rx.search(text):
                    hits.append(f"{f.relative_to(ROOT)}: {name}")        # never print the match itself
        self.assertEqual(hits, [], "possible secret(s) committed: " + "; ".join(hits))

    def test_detector_actually_detects(self):
        fake = "api_key = \"" + "a1b2c3d4e5f6a7b8c9d0" + "\""
        self.assertTrue(PATTERNS["hard-coded api key assignment"].search(fake))
        self.assertTrue(PATTERNS["zai-style key (id.secret)"].search("0" * 32 + ".AbCdEfGhIjKlMn"))


if __name__ == "__main__":
    unittest.main()
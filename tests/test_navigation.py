import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class NavigationTests(unittest.TestCase):
    def test_authenticated_pages_use_shared_navigation(self) -> None:
        pages = {
            "index.html": "editor",
            "stats.html": "stats",
            "compare.html": "compare",
            "moderate.html": "moderate",
        }
        for filename, active in pages.items():
            html = (ROOT / "static" / filename).read_text()
            self.assertIn(f'data-app-nav="{active}"', html)
            self.assertIn('/static/nav.js', html)
            self.assertIn('/static/nav.css', html)

        script = (ROOT / "static" / "nav.js").read_text()
        for path in ("/editor", "/stats", "/compare", "/moderate"):
            self.assertIn(path, script)


if __name__ == "__main__":
    unittest.main()

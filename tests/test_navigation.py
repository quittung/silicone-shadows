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
            self.assertIn('/static/nav.js?v=20260822-1', html)
            self.assertIn('/static/nav.css?v=20260822-1', html)

        script = (ROOT / "static" / "nav.js").read_text()
        for path in ("/editor", "/stats", "/compare", "/moderate"):
            self.assertIn(path, script)
        self.assertIn("root.scrollLeft +=", script)

    def test_compare_is_the_primary_landing_choice(self) -> None:
        html = (ROOT / "static" / "public.html").read_text()
        self.assertIn('<section id="compare-choice" class="choice">', html)
        self.assertIn('<a class="choice-action" href="/compare">', html)
        self.assertLess(html.index('id="compare-choice"'), html.index('id="catalog-choice"'))
        self.assertIn("Open comparison", html)
        self.assertIn("Open catalog editor", html)
        self.assertIn('id="age-gate"', html)
        script = (ROOT / "static" / "public.js").read_text()
        self.assertIn("silicone_shadows_age_confirmed", script)


if __name__ == "__main__":
    unittest.main()

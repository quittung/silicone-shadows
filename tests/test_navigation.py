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
            self.assertIn('/static/nav.js?v=20260822-2', html)
            self.assertIn('/static/nav.css?v=20260822-1', html)

        script = (ROOT / "static" / "nav.js").read_text()
        for path in ("/editor", "/stats", "/compare", "/moderate"):
            self.assertIn(path, script)
        self.assertIn("root.scrollLeft +=", script)
        self.assertIn("session.hosted && !session.user", script)

    def test_compare_is_the_primary_landing_choice(self) -> None:
        html = (ROOT / "static" / "public.html").read_text()
        self.assertIn('<section id="compare-choice" class="choice">', html)
        self.assertIn('<a class="choice-action" href="/compare">', html)
        self.assertLess(html.index('id="compare-choice"'), html.index('id="catalog-choice"'))
        self.assertIn("Open comparison", html)
        self.assertIn("Open catalog editor", html)

    def test_compare_gates_product_loading(self) -> None:
        public_html = (ROOT / "static" / "public.html").read_text()
        public_script = (ROOT / "static" / "public.js").read_text()
        compare_html = (ROOT / "static" / "compare.html").read_text()

        self.assertNotIn('id="age-gate"', public_html)
        self.assertNotIn("silicone_shadows_age_confirmed", public_script)
        self.assertIn('id="age-gate"', compare_html)
        self.assertIn("async function loadComparison()", compare_html)
        self.assertIn("silicone_shadows_age_confirmed", compare_html)
        self.assertIn("else ageGate.showModal();", compare_html)


if __name__ == "__main__":
    unittest.main()

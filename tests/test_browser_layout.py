import unittest

from playwright.sync_api import sync_playwright

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from html_gen import _build_html
from playwright_runtime import playwright_launch_env


class MobileBrowserLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, env=playwright_launch_env())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_mobile_toc_button_and_player_share_row_without_overlap(self):
        html = _build_html(
            "Episode",
            [
                (-1, None, "导览内容足够长，用于移动端页面测试。"),
                (0, "第一章", "正文内容。" * 300),
            ],
            word_count=1200,
            date_str="2026-08-03",
            mp3_url="episode.mp3",
        )
        for width in (320, 375, 430):
            with self.subTest(width=width):
                page = self.browser.new_page(
                    viewport={"width": width, "height": 740})
                page.set_content(html, wait_until="domcontentloaded")
                page.wait_for_timeout(50)

                def read_geometry():
                    return page.evaluate("""
                    () => {
                      const toggle = document.querySelector('.toc-toggle')
                        .getBoundingClientRect();
                      const player = document.querySelector('.player')
                        .getBoundingClientRect();
                      return {
                        toggleTop: toggle.top,
                        toggleRight: toggle.right,
                        playerTop: player.top,
                        playerLeft: player.left,
                        playerRight: player.right,
                        viewportWidth: innerWidth,
                        documentScrollWidth:
                        document.documentElement.scrollWidth,
                      };
                    }
                    """)

                for scroll_y in (0, 900):
                    if scroll_y:
                        page.evaluate(
                            "(y) => window.scrollTo(0, y)", scroll_y)
                        page.wait_for_timeout(50)
                    geometry = read_geometry()
                    self.assertLessEqual(
                        abs(geometry["toggleTop"] - geometry["playerTop"]), 1)
                    self.assertLessEqual(
                        geometry["toggleRight"], geometry["playerLeft"])
                    self.assertLessEqual(
                        geometry["playerRight"], geometry["viewportWidth"])
                    self.assertLessEqual(
                        geometry["documentScrollWidth"],
                        geometry["viewportWidth"])
                page.close()

    def test_mobile_open_toc_hides_player_and_keeps_back_link_clickable(self):
        html = _build_html(
            "Episode",
            [
                (-1, None, "导览内容足够长，用于移动端页面测试。"),
                (0, "第一章", "正文内容。" * 300),
            ],
            word_count=1200,
            date_str="2026-08-03",
            mp3_url="episode.mp3",
        )
        for width in (320, 375, 430):
            with self.subTest(width=width):
                page = self.browser.new_page(
                    viewport={"width": width, "height": 740})
                page.set_content(html, wait_until="domcontentloaded")
                page.locator(".toc-toggle").click()
                page.wait_for_timeout(400)

                back_link = page.locator(".back-link")
                box = back_link.bounding_box()
                self.assertIsNotNone(box)
                state = page.evaluate(
                    """
                    ({x, y}) => {
                      const hit = document.elementFromPoint(x, y);
                      const player = document.querySelector('.player');
                      return {
                        backLinkHit:
                          Boolean(hit && hit.closest('.back-link')),
                        playerVisibility:
                          window.getComputedStyle(player).visibility,
                      };
                    }
                    """,
                    {
                        "x": box["x"] + box["width"] / 2,
                        "y": box["y"] + box["height"] / 2,
                    },
                )
                self.assertTrue(state["backLinkHit"])
                self.assertEqual(state["playerVisibility"], "hidden")
                page.close()

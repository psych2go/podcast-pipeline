import unittest

from playwright.sync_api import sync_playwright

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from html_gen import _build_html
from site_index import (
    CARDS_END,
    CARDS_START,
    INDEX_TEMPLATE,
    STATS_END,
    STATS_START,
    _cards_html,
    _stats_html,
    replace_region,
)
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

    def test_toc_accessibility_state_tracks_breakpoint_and_escape(self):
        html = _build_html(
            "Episode",
            [
                (-1, None, "导览内容足够长，用于目录状态测试。"),
                (0, "第一章", "正文内容。" * 100),
            ],
            word_count=600,
            date_str="2026-08-03",
            mp3_url="episode.mp3",
        )
        page = self.browser.new_page(
            viewport={"width": 1100, "height": 740})
        page.set_content(html, wait_until="domcontentloaded")

        def toc_state():
            return page.evaluate("""
            () => {
              const toc = document.querySelector('.toc');
              return {
                ariaHidden: toc.getAttribute('aria-hidden'),
                inert: toc.inert,
                visibility: getComputedStyle(toc).visibility,
                overflow: document.body.style.overflow,
                expanded: document.querySelector('.toc-toggle')
                  .getAttribute('aria-expanded'),
                activeClass: toc.classList.contains('open'),
              };
            }
            """)

        closed = toc_state()
        self.assertEqual(closed["ariaHidden"], "true")
        self.assertTrue(closed["inert"])
        self.assertEqual(closed["visibility"], "hidden")
        self.assertEqual(closed["expanded"], "false")

        page.locator(".toc-toggle").click()
        page.wait_for_timeout(300)
        opened = toc_state()
        self.assertIsNone(opened["ariaHidden"])
        self.assertFalse(opened["inert"])
        self.assertEqual(opened["visibility"], "visible")
        self.assertEqual(opened["overflow"], "hidden")

        page.keyboard.press("Escape")
        self.assertEqual(page.evaluate("() => document.activeElement.id"),
                         "tocToggle")
        self.assertEqual(toc_state()["ariaHidden"], "true")

        page.set_viewport_size({"width": 1440, "height": 900})
        page.wait_for_timeout(250)
        desktop = toc_state()
        self.assertIsNone(desktop["ariaHidden"])
        self.assertFalse(desktop["inert"])
        self.assertTrue(desktop["activeClass"])
        self.assertEqual(desktop["overflow"], "")

        page.set_viewport_size({"width": 390, "height": 740})
        page.wait_for_timeout(250)
        mobile = toc_state()
        self.assertEqual(mobile["ariaHidden"], "true")
        self.assertTrue(mobile["inert"])
        self.assertFalse(mobile["activeClass"])
        page.close()

    def test_desktop_reading_column_and_toc_do_not_overlap(self):
        html = _build_html(
            (
                "Google's AI Brain Drain, SpaceX's Huge Quarter, "
                "Airtable's 90% Collapse, US Data Fuels China AI"
            ),
            [
                (-1, None, "导览内容足够长，用于桌面阅读版式测试。"),
                (0, "第一章：Google 失去的只是人才吗", "正文内容。" * 300),
                (1, "第二章：前沿智能为什么仍有溢价", "正文内容。" * 300),
            ],
            word_count=6200,
            date_str="2026-08-10",
            mp3_url="episode.mp3",
        )
        page = self.browser.new_page(
            viewport={"width": 1440, "height": 1000})
        page.set_content(html, wait_until="domcontentloaded")
        page.wait_for_timeout(100)
        geometry = page.evaluate("""
        () => {
          const title = document.querySelector('.hero h1')
            .getBoundingClientRect();
          const chapter = document.querySelector('.chapter:not(.chapter-intro)')
            .getBoundingClientRect();
          const paragraph = document.querySelector(
            '.chapter:not(.chapter-intro) p').getBoundingClientRect();
          const toc = document.querySelector('.toc').getBoundingClientRect();
          return {
            titleRight: title.right,
            titleBottom: title.bottom,
            chapterRight: chapter.right,
            paragraphWidth: paragraph.width,
            tocLeft: toc.left,
            tocRight: toc.right,
            viewportWidth: innerWidth,
            documentScrollWidth: document.documentElement.scrollWidth,
          };
        }
        """)
        self.assertLessEqual(
            geometry["titleRight"], geometry["viewportWidth"])
        self.assertLess(geometry["titleBottom"], 600)
        self.assertLessEqual(geometry["paragraphWidth"], 672)
        self.assertLessEqual(geometry["chapterRight"], geometry["tocLeft"])
        self.assertLessEqual(
            geometry["tocRight"], geometry["viewportWidth"])
        self.assertLessEqual(
            geometry["documentScrollWidth"], geometry["viewportWidth"])
        page.close()


class HomepageBrowserLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, env=playwright_launch_env())
        cls.entries = [
            {
                "folder": f"Episode {index}",
                "title": (
                    "A Long English Podcast Title About Evidence and Memory "
                    f"Number {index}"
                ),
                "path": f"episode-{index}",
                "duration": 20 + index,
                "words": 6000 + index * 100,
                "source_name": "Example Podcast",
                "source_url": "https://example.com/source",
            }
            for index in range(1, 5)
        ]
        page_html = replace_region(
            INDEX_TEMPLATE, STATS_START, STATS_END,
            _stats_html(cls.entries),
        )
        cls.page_html = replace_region(
            page_html, CARDS_START, CARDS_END,
            _cards_html(cls.entries),
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_homepage_has_no_horizontal_overflow(self):
        for width in (320, 390, 768, 1440):
            with self.subTest(width=width):
                page = self.browser.new_page(
                    viewport={"width": width, "height": 900})
                page.set_content(
                    self.page_html, wait_until="domcontentloaded")
                geometry = page.evaluate("""
                () => ({
                  viewportWidth: innerWidth,
                  documentScrollWidth: document.documentElement.scrollWidth,
                  titleWidth: document.querySelector('.hero h1')
                    .getBoundingClientRect().width,
                  cardWidth: document.querySelector('.episode-card')
                    .getBoundingClientRect().width,
                })
                """)
                self.assertLessEqual(
                    geometry["documentScrollWidth"],
                    geometry["viewportWidth"],
                )
                self.assertGreater(geometry["titleWidth"], 0)
                self.assertGreater(geometry["cardWidth"], 0)
                page.close()

    def test_homepage_search_updates_count_and_empty_state(self):
        page = self.browser.new_page(
            viewport={"width": 1280, "height": 900})
        page.set_content(self.page_html, wait_until="domcontentloaded")
        search = page.locator("#episodeSearch")
        search.focus()
        focus_color = page.locator(".search-wrap").evaluate(
            "element => getComputedStyle(element).borderBottomColor")
        self.assertEqual(focus_color, "rgb(40, 83, 70)")
        search.fill("Number 2")
        self.assertEqual(page.locator(".episode-card:visible").count(), 1)
        self.assertEqual(page.locator("#visibleCount").text_content(), "1")
        search.fill("not-found")
        self.assertTrue(page.locator("#emptyState").is_visible())
        page.locator("#searchClear").click()
        self.assertEqual(
            page.locator(".episode-card:visible").count(),
            len(self.entries),
        )
        page.close()

"""
tests/test_ui_keyboard_browser.py

Real-browser verification of the Strategy Templates grid's official
keyboard workflow: click a cell once, then type-and-Tab across the
whole row (Label -> Market -> Interval -> weight columns) without
touching the mouse again, using Enter only on the row's last cell to
commit it and drop to the next row. AppTest (streamlit.testing.v1)
cannot drive st.data_editor's canvas-rendered cells at all -- this is
the only layer that actually exercises Tab/Enter/typing against the
real widget, so it is deliberately a Playwright test against a live
`streamlit run` process rather than another AppTest-based check.

This module is intentionally skipped, not failed, when Playwright or a
Chromium binary isn't available in the current environment -- the same
convention `test_live_connection.py` already uses for a capability
(a live LSEG session) most environments running `pytest -q` don't have.
Where Chromium is available, this is expected to pass and is the only
authority for any claim about grid keyboard behavior; do not report
Tab/Enter behavior as verified without actually running this.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

playwright_sync_api = pytest.importorskip("playwright.sync_api")

from strategy_sets.repository import StrategySetRepository

_CHROMIUM_CANDIDATES = [
    os.environ.get("OSCILL8_TEST_CHROMIUM", ""),
    "/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
]


def _chromium_executable() -> str | None:
    for candidate in _CHROMIUM_CANDIDATES:
        if candidate and os.path.exists(candidate):
            return candidate
    return shutil.which("chromium") or shutil.which("chromium-browser")


_CHROMIUM_PATH = _chromium_executable()
if _CHROMIUM_PATH is None:
    pytest.skip("No Chromium binary available for browser-driven keyboard test", allow_module_level=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_up(url: str, timeout_seconds: float = 30.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.3)
    raise RuntimeError(f"Streamlit app never came up at {url}")


@pytest.fixture
def running_app(tmp_path):
    """Starts the real `streamlit run ui/app.py` process, isolated to a
    tmp_path Strategy Set / SQLite cache directory (never the real
    data/strategy_sets/), and tears it down afterward."""
    port = _free_port()
    repo_dir = tmp_path / "strategy_sets"
    env = dict(os.environ)
    env["RBS_STRATEGY_SETS_DIR"] = str(repo_dir)
    env["RBS_SQLITE_PATH"] = str(tmp_path / "oscill8.db")

    app_path = Path(__file__).resolve().parent.parent / "ui" / "app.py"
    process = subprocess.Popen(
        [
            sys.executable, "-m", "streamlit", "run", str(app_path),
            "--server.headless", "true",
            "--server.port", str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_until_up(f"http://localhost:{port}")
        yield f"http://localhost:{port}", StrategySetRepository(base_dir=str(repo_dir))
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def _type_and_tab(page, text: str, wait_ms: int = 60) -> None:
    """Types `text` as individually-dispatched keystrokes (not
    Playwright's batched `.type()`) -- verified empirically that
    `.type()`'s rapid-fire keydown burst can race the grid's
    click-to-edit-mode transition and drop the first character even
    after an arbitrarily long prior wait, while naturally-paced
    individual keypresses (as a real user would type) never do. Ends
    with Tab -- the official commit-and-move-right workflow."""
    for char in text:
        page.keyboard.press("Space" if char == " " else char)
        page.wait_for_timeout(wait_ms)
    page.wait_for_timeout(200)
    page.keyboard.press("Tab")
    page.wait_for_timeout(300)


def test_full_row_entry_via_tab_only_no_mouse_between_cells(running_app):
    """Click the Label cell once, then Tab-chain Label -> Market ->
    Interval -> weight columns with no further mouse interaction,
    using an ArrowRight (grid navigation, not editing) to skip a
    weight column, and Enter only on the final cell to commit it and
    drop to the next row. Verifies via the real "Save Strategy Set"
    button + a filesystem read-back of the saved StrategySet -- a
    reliable, non-visual assertion rather than screenshot inspection."""
    base_url, repo = running_app

    with playwright_sync_api.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=_CHROMIUM_PATH, headless=True)
        page = browser.new_page(viewport={"width": 1700, "height": 1300})
        page.goto(base_url, wait_until="load")
        page.wait_for_timeout(6000)

        grid = page.locator("canvas").nth(0)
        box = grid.bounding_box()
        assert box is not None, "Strategy Templates grid did not render"

        # Column boundaries measured from the rendered grid: Label,
        # Market, Interval, then six equal-width weight columns.
        widths = [167, 134, 134, 134, 134, 134, 134, 134, 134]
        header_h = box["height"] / 3
        row_h = box["height"] / 3
        x0, y0 = box["x"], box["y"]

        def click_cell(col_index: int, row_index: int) -> None:
            cx = x0 + sum(widths[:col_index]) + widths[col_index] / 2
            cy = y0 + header_h + row_index * row_h + row_h / 2
            page.mouse.click(cx, cy)

        # Single mouse interaction for the whole row: the initial click.
        click_cell(0, 0)
        page.wait_for_timeout(300)

        _type_and_tab(page, "Test Fly")   # Label -> Tab -> Market cell
        _type_and_tab(page, "SOFR")       # Market -> Tab -> Interval cell
        _type_and_tab(page, "DAILY")      # Interval -> Tab -> weight col 1

        # Grid-navigation Arrow key while NOT editing: skip weight col 1
        # (leave it blank) and land on weight col 2 instead of typing.
        page.keyboard.press("ArrowRight")
        page.wait_for_timeout(200)

        _type_and_tab(page, "5")          # weight col 2 -> Tab -> weight col 3

        # Last cell of the row: Enter commits it AND drops to the next
        # row, exactly as the official workflow says it should.
        for char in "-10":
            page.keyboard.press(char)
            page.wait_for_timeout(60)
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        page.wait_for_timeout(400)

        # Save via the real dialog: name it, click Save.
        save_button = page.get_by_text("Save Strategy Set", exact=True)
        save_button.click()
        page.wait_for_timeout(500)
        name_input = page.get_by_label("Strategy Set Name")
        name_input.click()
        page.wait_for_timeout(200)
        for char in "Keyboard Test Set":
            page.keyboard.press("Space" if char == " " else char)
            page.wait_for_timeout(30)
        page.wait_for_timeout(200)
        page.get_by_role("button", name="Save", exact=True).click()
        page.wait_for_timeout(1000)

        browser.close()

    saved = repo.load("Keyboard Test Set")
    assert len(saved.entries) == 1
    entry = saved.entries[0]
    assert entry.name == "Test Fly"
    assert entry.definition.market_key == "SOFR"
    from core.config import BarInterval
    assert entry.definition.interval == BarInterval.DAILY
    # dense [0(skipped by ArrowRight), 5, -10, 0, 0, 0] -> leading zero
    # re-based away -> offsets (0, 1), weights (5.0, -10.0).
    assert entry.definition.offsets == (0, 1)
    assert entry.definition.weights == (5.0, -10.0)

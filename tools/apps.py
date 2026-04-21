"""App management tools - launching and interacting with apps."""
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from hud.server import MCPRouter
from tools.browser import http_client, BACKEND_URL, playwright

logger = logging.getLogger(__name__)

router = MCPRouter()


async def _launch_app_internal(app_name: str) -> dict:
    """Internal function to launch an app and navigate to it.

    Args:
        app_name: Name of the app to launch (e.g., 'todo', '2048')

    Returns:
        Dict with app info (name, url, frontend_port, backend_port)

    Raises:
        ValueError: If app not found
        RuntimeError: If launch fails
        TimeoutError: If launch times out
        ConnectionError: If cannot connect to backend
    """
    try:
        response = await http_client.post(
            "/apps/launch",
            json={"app_name": app_name},
            timeout=60.0,
        )

        if response.status_code == 404:
            raise ValueError(f"App '{app_name}' not found")
        elif response.status_code != 200:
            raise RuntimeError(f"Failed to launch app: {response.text}")
    except httpx.ReadTimeout:
        raise TimeoutError(f"Timeout launching app '{app_name}'. Try again in a few seconds.")
    except httpx.ConnectError:
        raise ConnectionError(f"Could not connect to backend at {BACKEND_URL}")

    app_info = response.json()
    app_url = app_info["url"]

    # Navigate to the app
    try:
        await playwright(action="navigate", url=app_url, wait_for_load_state="networkidle")
        await asyncio.sleep(1)
    except Exception as e:
        logger.warning("Could not auto-navigate to app: %s", e)

    return app_info


@router.tool
async def launch_app(app_name: str) -> str:
    """Launch a specific application dynamically and navigate to it.

    Args:
        app_name: Name of the app to launch (e.g., 'todo', '2048')

    Returns:
        Success message with app URL
    """
    try:
        app_info = await _launch_app_internal(app_name)
        return f"Launched {app_name} at {app_info['url']}"
    except (ValueError, RuntimeError, TimeoutError, ConnectionError) as e:
        return str(e)
    except Exception as e:
        return f"Error launching app '{app_name}': {str(e)}"


@router.tool
async def api_request(url: str, method: str = "GET", data: dict | None = None) -> dict:
    """Make HTTP API requests.

    Args:
        url: The URL to request
        method: HTTP method (GET, POST, etc.)
        data: Optional JSON data for POST/PUT requests

    Returns:
        Response data as dict
    """
    async with httpx.AsyncClient() as client:
        response = await client.request(method, url, json=data)
        return {
            "status": response.status_code,
            "data": response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else response.text,
        }


# ---------------------------------------------------------------------------
# NTUMods scenario helpers (curriculum lookup + live timetable state read)
# ---------------------------------------------------------------------------

_CURRICULUM_DIR = Path(__file__).resolve().parent.parent / "data" / "curriculum"
_CURRICULUM_CACHE: dict[str, dict[str, Any]] = {}


def _load_curriculum(program: str) -> dict[str, Any]:
    if program in _CURRICULUM_CACHE:
        return _CURRICULUM_CACHE[program]
    filename_map = {"CCDS-CSC": "ccds_ay25-26_csc.json"}
    fname = filename_map.get(program)
    if not fname:
        raise ValueError(f"Unknown programme '{program}'. Known: {list(filename_map)}")
    path = _CURRICULUM_DIR / fname
    if not path.exists():
        raise FileNotFoundError(f"Curriculum file missing: {path}")
    data = json.loads(path.read_text())
    _CURRICULUM_CACHE[program] = data
    return data


def _find_sem(curriculum: dict[str, Any], year: int, sem: int) -> dict[str, Any] | None:
    for block in curriculum.get("semesters", []):
        if block["year"] == year and block["sem"] == sem:
            return block
    return None


@router.tool
async def get_curriculum(program: str = "CCDS-CSC", year: int = 1, sem: int = 1) -> dict:
    """Return required modules, electives, and recommended AU for the given
    programme/year/sem, plus the NEXT sem (so modules can be pulled forward
    when the student wants extra AU this sem).

    Args:
        program: Programme code (currently only 'CCDS-CSC' is bundled).
        year: 1-4.
        sem: 1 or 2.

    Returns:
        Dict with keys: programme, ay, current_sem, next_sem (nullable),
        mpe_structure, au_summary, notes.
    """
    curriculum = _load_curriculum(program)
    current = _find_sem(curriculum, year, sem)
    if current is None:
        return {"error": f"No sem block for year={year} sem={sem}"}

    if sem == 1:
        nxt_year, nxt_sem = year, 2
    else:
        nxt_year, nxt_sem = year + 1, 1
    next_block = _find_sem(curriculum, nxt_year, nxt_sem)

    return {
        "programme": curriculum["programme"],
        "ay": curriculum["ay"],
        "current_sem": current,
        "next_sem": next_block,
        "mpe_structure": curriculum.get("mpe_structure"),
        "au_summary": curriculum.get("au_summary"),
        "notes": curriculum.get("notes", []),
    }


# JS scraper for the NTUMods SPA. Tries common patterns:
#   1. URL query params (most timetable sites encode selections there)
#   2. Visible schedule blocks in the DOM
_READ_TIMETABLE_JS = r"""
(() => {
    const result = {
        url: window.location.href,
        modules_from_url: {},
        blocks: [],
    };
    try {
        const u = new URL(window.location.href);
        for (const [k, v] of u.searchParams.entries()) {
            if (/^[A-Z]{2,3}\d{3,4}[A-Z]?$/.test(k)) {
                result.modules_from_url[k] = v;
            }
        }
    } catch (e) { /* ignore */ }

    const candidates = document.querySelectorAll(
        '[class*="timetable"] [class*="cell"], [class*="schedule"] [class*="block"], ' +
        '[data-module], [data-code], [data-index], .lesson, .class-block'
    );
    candidates.forEach((el) => {
        const text = (el.innerText || '').trim();
        if (!text) return;
        result.blocks.push({
            text: text.slice(0, 120),
            dataset: Object.assign({}, el.dataset || {}),
            rect: el.getBoundingClientRect
                ? (() => { const r = el.getBoundingClientRect(); return { x: r.x, y: r.y, w: r.width, h: r.height }; })()
                : null,
        });
    });
    return result;
})()
"""


@router.tool
async def read_timetable_state() -> dict:
    """Scrape the current NTUMods timetable state from the live page.

    Returns the page URL, any module codes visible in URL query params,
    and text/dataset of likely schedule blocks in the DOM. The scenario
    evaluator uses this to compute the final reward. The agent can also
    call it mid-run to self-check progress.

    Returns:
        Dict with keys: url, modules_from_url, blocks.
    """
    try:
        result = await playwright(action="evaluate", script=_READ_TIMETABLE_JS)
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except Exception:
                return {"raw": result}
        return {"raw": str(result)}
    except Exception as e:
        logger.warning("read_timetable_state failed: %s", e)
        return {"error": str(e)}


__all__ = [
    "router",
    "launch_app",
    "_launch_app_internal",
    "api_request",
    "get_curriculum",
    "read_timetable_state",
    "_load_curriculum",
    "_find_sem",
]

"""NTUMods scenarios - build a valid NTU timetable on https://ntumods.org."""
import logging
import re
from typing import Any

from tools.apps import _load_curriculum, _find_sem, read_timetable_state
from tools.browser import playwright

logger = logging.getLogger(__name__)

NTUMODS_URL = "https://ntumods.org"


def _build_prompt(
    year: int,
    sem: int,
    au_goal: int,
    recommended_au: int,
    sem_block: dict,
    next_sem_block: dict | None,
    preferences: str,
) -> str:
    required_lines = [
        f"- {c['code']}: {c['name']} ({c['au']} AU, {c['type']})"
        for c in sem_block.get("courses", [])
    ]
    next_lines = []
    if next_sem_block:
        next_lines = [
            f"- {c['code']}: {c['name']} ({c['au']} AU, {c['type']})"
            for c in next_sem_block.get("courses", [])
        ]

    pref_line = preferences.strip() or "(none)"

    return (
        f"You are helping a Year {year} Sem {sem} NTU CCDS (Computer Science) student "
        f"build their class timetable on https://ntumods.org.\n\n"
        f"Target total AU: {au_goal} (recommended for this sem: {recommended_au})\n"
        f"User preferences: {pref_line}\n\n"
        f"Courses for this sem (per curriculum):\n" + "\n".join(required_lines) + "\n\n"
        + (
            "Courses available next sem (may be pulled forward if the user wants extra AU):\n"
            + "\n".join(next_lines) + "\n\n"
            if next_lines
            else ""
        )
        + "Steps to follow:\n"
        "1. Take a screenshot. You should see the NTUMods landing page.\n"
        "2. Click the 'Get Started' button to open the timetable builder.\n"
        "3. For each course above, search by code, click to add, then pick an index.\n"
        "4. If a chosen index clashes with another, click the clashing block to open "
        "the index picker and try an alternative timing.\n"
        "5. Keep iterating until every required course is added, total AU equals the "
        "target, and there are no clashes.\n"
        "6. Call the `get_curriculum` tool any time to recall codes & AU.\n"
        "7. Call the `read_timetable_state` tool to self-check progress without "
        "taking a screenshot.\n\n"
        "Start by taking a screenshot."
    )


def _parse_preferences(preferences: str) -> dict:
    """Very light rule-based preference extraction. Unknown prefs are ignored."""
    p = (preferences or "").lower()
    rules: dict[str, Any] = {"blocked_days": set(), "no_morning": False, "no_evening": False}
    for day, short in [
        ("monday", "Mon"), ("tuesday", "Tue"), ("wednesday", "Wed"),
        ("thursday", "Thu"), ("friday", "Fri"), ("saturday", "Sat"), ("sunday", "Sun"),
    ]:
        if re.search(rf"\bno\s+{day[:3]}\w*\b", p):
            rules["blocked_days"].add(short)
    if "no morning" in p:
        rules["no_morning"] = True
    if "no evening" in p or "no night" in p:
        rules["no_evening"] = True
    return rules


def _extract_module_codes(state: dict, known_codes: set[str]) -> set[str]:
    found: set[str] = set()
    for code in state.get("modules_from_url", {}).keys():
        found.add(code.upper())
    for block in state.get("blocks", []) or []:
        text = (block.get("text") or "").upper()
        for code in known_codes:
            if code.upper() in text:
                found.add(code.upper())
        ds = block.get("dataset") or {}
        for v in ds.values():
            if isinstance(v, str) and v.upper() in known_codes:
                found.add(v.upper())
    return found


def _score(
    state: dict,
    sem_block: dict,
    next_sem_block: dict | None,
    au_goal: int,
    preferences: str,
) -> tuple[float, dict]:
    required_codes = {
        c["code"].upper()
        for c in sem_block.get("courses", [])
        if c.get("code") and not c["code"].startswith("SC3xxx") and c["code"] not in ("BDE", "CSL")
    }
    next_codes = set()
    if next_sem_block:
        next_codes = {
            c["code"].upper()
            for c in next_sem_block.get("courses", [])
            if c.get("code") and not c["code"].startswith("SC3xxx") and c["code"] not in ("BDE", "CSL")
        }
    known = required_codes | next_codes
    found = _extract_module_codes(state, known)

    code_to_au = {
        c["code"].upper(): (c.get("au") or 0)
        for c in (sem_block.get("courses", []) + (next_sem_block.get("courses", []) if next_sem_block else []))
        if c.get("code")
    }
    total_au = sum(code_to_au.get(code, 0) for code in found)

    # Short-circuit: agent did nothing measurable → 0.0, no participation credit.
    if not found and total_au == 0:
        return 0.0, {
            "total_au": 0,
            "au_goal": au_goal,
            "found_codes": [],
            "reward": 0.0,
            "reason": "no modules detected on page",
        }

    # 1. AU match (0.40)
    au_diff = abs(total_au - au_goal)
    au_score = max(0.0, 1.0 - au_diff / 3.0)

    # 2. Required coverage (0.30)
    if required_codes:
        required_score = len(found & required_codes) / len(required_codes)
    else:
        required_score = 1.0

    # 3. Clash-free (0.20) — without a reliable slot parse we treat presence of a
    # "clash"/"conflict" token anywhere on the page as evidence of a clash.
    clash_text_hits = 0
    for block in state.get("blocks", []) or []:
        t = (block.get("text") or "").lower()
        if "clash" in t or "conflict" in t:
            clash_text_hits += 1
    clash_score = 0.0 if clash_text_hits else 1.0

    # 4. Preferences (0.10) — placeholder: full credit unless we see a blocked
    # day name in a visible block that also contains a known module code.
    rules = _parse_preferences(preferences)
    pref_score = 1.0
    if rules["blocked_days"]:
        for block in state.get("blocks", []) or []:
            text = block.get("text") or ""
            if any(d in text for d in rules["blocked_days"]) and any(c in text.upper() for c in found):
                pref_score = 0.0
                break

    reward = 0.40 * au_score + 0.30 * required_score + 0.20 * clash_score + 0.10 * pref_score
    breakdown = {
        "total_au": total_au,
        "au_goal": au_goal,
        "au_score": au_score,
        "required_codes": sorted(required_codes),
        "found_codes": sorted(found),
        "required_score": required_score,
        "clash_score": clash_score,
        "pref_score": pref_score,
        "reward": reward,
    }
    return reward, breakdown


def register_scenarios(env: Any) -> None:
    """Register NTUMods scenarios with the environment."""

    @env.scenario("ntumods-build-schedule")
    async def build_schedule(
        program: str = "CCDS-CSC",
        year: int = 1,
        sem: int = 1,
        target_au: int | None = None,
        preferences: str = "",
    ) -> Any:
        """Build a valid timetable for the given NTU CCDS student profile.

        Args:
            program: Programme code (currently 'CCDS-CSC').
            year: Year of study (1-4).
            sem: Semester (1 or 2).
            target_au: Desired total AU. If None, uses curriculum recommended.
            preferences: Free-text preferences, e.g. "no Friday classes".
        """
        setup_error: str | None = None
        curriculum = None
        sem_block = None
        next_sem_block = None

        try:
            curriculum = _load_curriculum(program)
        except Exception as e:
            setup_error = f"Could not load curriculum for {program!r}: {e}"
            logger.error(setup_error)

        if curriculum is not None:
            sem_block = _find_sem(curriculum, year, sem)
            if sem_block is None:
                setup_error = f"No curriculum entry for year={year} sem={sem}"
                logger.error(setup_error)

        if sem_block is not None and curriculum is not None:
            nxt_year, nxt_sem = (year, 2) if sem == 1 else (year + 1, 1)
            next_sem_block = _find_sem(curriculum, nxt_year, nxt_sem)

        recommended_au = (sem_block.get("total_au") if sem_block else None) or 19
        au_goal = int(target_au) if target_au is not None else recommended_au

        if setup_error is not None:
            yield (
                "Scenario setup failed: " + setup_error + "\n"
                "Please report this to the maintainer."
            )
            yield 0.0
            return

        try:
            await playwright(
                action="navigate",
                url=NTUMODS_URL,
                wait_for_load_state="networkidle",
            )
        except Exception as e:
            logger.warning("Navigation to ntumods.org failed: %s", e)

        logger.info(
            "NTUMods scenario: year=%d sem=%d target_au=%d (recommended=%d) prefs=%r",
            year, sem, au_goal, recommended_au, preferences,
        )

        prompt = _build_prompt(
            year, sem, au_goal, recommended_au, sem_block, next_sem_block, preferences
        )

        _ = yield prompt

        try:
            state = await read_timetable_state()
        except Exception as e:
            logger.error("read_timetable_state failed: %s", e)
            yield 0.0
            return

        reward, breakdown = _score(state, sem_block, next_sem_block, au_goal, preferences)
        logger.info("NTUMods result: %s", breakdown)
        yield reward

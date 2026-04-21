# CLAUDE.md

## Project purpose

HUD browser-automation environment. An AI agent (Claude / GPT via hud.ai) drives a
real Chrome through CDP to build NTU CCDS timetables on https://ntumods.org
given a student profile (year, sem, target AU, preferences).

The repo also ships two demo scenarios (`2048-reach-tile`, `todo-complete`) from
upstream `hud-evals/hud-browser` that we keep around as sanity checks.

## Key files

- `env.py` — MCP server entry; registers tools and scenarios.
- `tools/browser.py` — `playwright`, `computer` tools (existing upstream).
- `tools/apps.py` — `launch_app`, `api_request`, plus project-specific tools
  `get_curriculum` and `read_timetable_state`.
- `scenarios/ntumods.py` — `ntumods-build-schedule` scenario, prompt builder, reward.
- `scenarios/game_2048.py`, `scenarios/todo.py` — legacy demo scenarios.
- `data/curriculum/ccds_ay25-26_csc.json` — hand-authored curriculum; source of
  truth for required courses + recommended AU per sem.
- `backend/server.py` — FastAPI managing Xvfb + BrowserOS Chrome (Linux-only);
  not used for NTUMods beyond providing Chrome.
- `Dockerfile.hud` — image hud.ai builds on every push.
- `remote_tasks.json` — local task list for `hud eval ./remote_tasks.json`.

## How to run evals

1. Push to GitHub. hud.ai auto-rebuilds the environment image (~4 min).
2. **Preferred:** hud.ai UI → Environments → `ntumods-scheduler` → pick scenario
   `ntumods-build-schedule` → set args → pick model (Claude Sonnet 4.6 works
   well; `claude-3-7-sonnet-20250219` is retired — do not select it) → Run.
3. CLI alternative: `hud eval ./remote_tasks.json --model claude-sonnet-4-6 --remote`.
4. Every run produces a trace URL with screenshots and tool calls.

## Updating curriculum data

`data/curriculum/ccds_ay25-26_csc.json` is hand-authored from the NTU CCDS PDF
(https://www.ntu.edu.sg/docs/librariesprovider118/ug/cs/ay2025/ccds_ay25-26_csc.pdf).
To add new programmes, add another JSON file alongside and extend the
`filename_map` in `tools/apps.py:_load_curriculum`.

## Development loop

- Local `hud dev` is blocked on macOS (backend uses Xvfb/X11). Iterate via:
  **edit → commit → push → wait for hud.ai build → run eval**.
- If you change `tools/` or `scenarios/`, no `backend/` changes are needed —
  the image rebuilds in <4 min because earlier layers are cached.
- If you change `backend/`, the Next.js app layers rebuild too (~6 min).

## Scoring (for `ntumods-build-schedule`)

Weighted reward in [0, 1]:
- 0.40 AU match (full credit at exact target, zero at ±3 AU off)
- 0.30 required-course coverage (curriculum JSON)
- 0.20 no clashes (heuristic: no "clash"/"conflict" text in visible blocks)
- 0.10 preferences (simple `"no friday"`, `"no morning"` style parsing)

See `scenarios/ntumods.py:_score` for the exact formula.

## Known gaps

- `read_timetable_state` uses a best-effort DOM + URL scrape. If ntumods.org
  changes markup it may return empty. The agent can always fall back to
  screenshots + reasoning.
- Curriculum JSON only covers CCDS-CSC AY25–26. Other programmes are TODO.
- Preference parser is keyword-based; anything beyond `"no <day>"` /
  `"no morning"` / `"no evening"` scores full marks by default.

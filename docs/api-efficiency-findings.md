# API efficiency findings — #1-#4

Written 2026-07-30 alongside implementing #1 (overnight missing
`stop_at_first_kill`) and #2 (uncached per-guild report list, fetched twice
for "Both"). #3 and #4 below were researched here first, then implemented
the same day.

**Status: #1, #2, #3 done and live. #4 implemented, then reverted the same
day after a real production regression** — see "Reverted" note at the end of
that section. The mocked tests for #4 passed (correct data, correct
independent-pagination handling) and a live single-report test was fast, but
a real single-guild run (19 reports, both metrics) hit a reproducible 20-30s
stall between the deaths and damage passes, and the results tables didn't
render. Root cause not confirmed before reverting — priority was restoring
working behavior for an actively-blocked user over debugging further with it
broken. The combine machinery (`_fetch_both_metrics` flag,
`_fetch_death_and_damage_events`, the aliased query) was fully removed
rather than left disabled, so there's no dead/risky code path lying around.

## #3 — Merge fights + actors (+ class/specs) into fewer round trips

**Where:** `src/report_cache.py`'s `get_report_fights` / `get_report_actors`
(→ `deaths_fetcher.fetch_all_fights` / `deaths_fetcher._fetch_player_actors`),
and `get_report_class_specs` (→ `player_details_fetcher.get_player_class_specs`).
All three are called once per report from `multi_guild._analyse_report()`
and from the single-guild flow in `ui/app.py`.

**What's wasteful:** `fetch_all_fights()` and `_fetch_player_actors()` are
both scoped to `report(code: $code)` with **zero interdependency** — no
shared arguments, neither needs the other's result — yet they're issued as
two fully sequential HTTP round trips per report. `get_player_class_specs()`
*does* need `fight_ids` (from the fights call), so it can't join a combined
fights+actors query, but it could immediately follow one.

**Proposed fix:** combine fights + actors into one GraphQL query (sibling
fields, no aliasing needed since the field names differ):

```graphql
query ($code: String!) {
  reportData {
    report(code: $code) {
      fights { id name encounterID difficulty kill startTime endTime friendlyPlayers }
      masterData {
        actors(type: "Player") { id name }
      }
    }
  }
}
```

Add a new fetcher (e.g. `_fetch_fights_and_actors(report_code)` in
`deaths_fetcher.py` or a new shared module) returning both, and a matching
`report_cache` entry point that populates `_fights_cache` and
`_actors_cache` from one call instead of two. `get_report_class_specs`
follows right after with the fight_ids now available — 3 round trips per
report → 2.

**Impact:** cuts round-trip count for *every* report across all three modes
(single-guild, live multi-guild, overnight) by up to a third. High
confidence on wall-clock/request-count reduction (fewer round trips, less
429 exposure from sheer request volume). **Moderate confidence on points
savings** — WCL's points formula is understood to scale primarily with
event/data volume fetched, not request count, so this may be more of a
speed/reliability win than a budget win. Worth a quick before/after
`get_rate_limit()` delta check on a real report before assuming it moves
the points needle.

**Risk:** low. Same data, same shape, no filtering/semantics change —
should be a safe, mechanical refactor.

## #4 — Combine Deaths + DamageTaken events into one request per page (situational)

**Where:** `src/deaths_fetcher.py`'s `_fetch_death_events()` and
`src/damage_taken_fetcher.py`'s `_fetch_damage_taken_events()`, both called
once per (report, boss, ability) — but on **separate top-level passes**
when the user selects "Both" (deaths + hits), driven by the
`for metric_is_deaths in metrics:` loop in `overnight.py` / the
`_passes` loop in `live_runner.py` / the two `compute_and_cache_results()`
calls in `ui/app.py`.

**What's wasteful:** WCL's `events` field takes a single `dataType` value —
you cannot request `dataType: [Deaths, DamageTaken]` in one field call — so
two genuinely separate event streams are unavoidable. But they *can* be
combined into **one HTTP request** via GraphQL aliasing, since both are
sibling reads off the same `report(code: $code)` node:

```graphql
query (
  $code: String!, $start: Float!, $end: Float!, $fightIDs: [Int!],
  $wipeCutoff: Int, $deathsAbilityId: Float, $damageAbilityId: Float,
  $fetchDeaths: Boolean!, $fetchDamage: Boolean!
) {
  reportData {
    report(code: $code) {
      deathEvents: events(
        startTime: $start, endTime: $end, dataType: Deaths,
        fightIDs: $fightIDs, wipeCutoff: $wipeCutoff, abilityID: $deathsAbilityId
      ) @include(if: $fetchDeaths) { data nextPageTimestamp }
      damageEvents: events(
        startTime: $start, endTime: $end, dataType: DamageTaken,
        fightIDs: $fightIDs, abilityID: $damageAbilityId
      ) @include(if: $fetchDamage) { data nextPageTimestamp }
    }
  }
}
```

**The hard part — independent pagination:** each aliased stream has its own
`nextPageTimestamp`. A combined query can't share one cursor across both.
The correct implementation:

1. Track two cursors, `next_start_deaths` and `next_start_damage`,
   independently.
2. On each request, include `@include(if: ...)` (or omit the alias
   entirely from the query string) for whichever stream(s) still have more
   pages — once a stream's `nextPageTimestamp` comes back null, stop
   requesting it, don't just stop advancing its cursor (that would
   silently re-fetch/duplicate its last page forever).
3. Only stop looping once *both* streams are exhausted.
4. This only pays off when **both** metrics are actually being fetched for
   the same report/ability at the same time — needs the calling code
   restructured so a report's death-events and damage-events fetch happen
   together (currently they're on separate top-level passes, see above),
   not just the query itself changed.

**Impact:** saves one HTTP round trip per *page* for reports that need
both streams — but most individual wipe-night reports likely only need one
page per event type in the first place (WCL's page size is generous
relative to a typical evening's event count), so the win is probably
concentrated in unusually long/event-heavy reports rather than uniform
across the board. Likely the smallest of the four findings in aggregate
impact, and the most implementation/testing effort (the independent-cursor
logic is exactly the kind of thing that silently drops or duplicates data
if not tested carefully against a multi-page report).

**Recommendation:** do this last, and only if #1–#3 don't already bring
points usage down far enough. Test explicitly against a report large enough
to force multi-page pagination on at least one of the two streams before
trusting it.

**As implemented, then reverted (2026-07-30):** point 4 above turned out
avoidable — all three callers already run the deaths pass before the damage
pass, with a fixed, verified ordering (`overnight._metrics_from_job` returns
`[True, False]`; `live_runner`'s `metrics.append(True)` then
`.append(False)`; `ui/app.py`'s `if show_deaths: ... if show_damage: ...`) —
so instead of restructuring `_analyse_report()`/`aggregate_guilds()` to fetch
both metrics per report in one pass, `report_cache.py` grew a module-level
`_fetch_both_metrics` flag (set via `reset_report_caches(fetch_both_metrics=True)`)
that `get_death_events()` checked: when true and damage wasn't cached yet for
that report/boss/ability, it fetched both via the combined query above and
warmed both caches; `get_damage_events()` needed zero changes.

Mocked tests passed cleanly (single-metric path provably untouched;
multi-page case — deaths needing 2 pages, damage exhausting after 1 —
correctly dropped the exhausted alias from later requests and produced
byte-identical assembled results to the old separate-fetch approach), and an
isolated live single-report test against the real API was fast (0.3s) with
correct data.

**But a real end-to-end single-guild run (guild 235490, 19 reports, both
Deaths and Hits) reproduced a 20-30 second stall between the deaths pass
finishing and the damage pass's results appearing** — the old behavior had
essentially no gap between passes. The results tables also failed to render
in the UI afterward. Root cause not confirmed: candidates considered were
GIL/thread-pool interaction under real concurrency (6 report-workers × the
larger combined query), some WCL-side behavior specific to the `@include`
directive under load, or something in how Streamlit's script re-execution
interacted with the delay — none confirmed before reverting. Given a user
was actively blocked by this, the fix was to revert immediately (all three
callers back to plain `reset_report_caches()`, `get_death_events`/
`get_damage_events` back to their original independent single-fetch forms,
`_fetch_both_metrics`/`_fetch_death_and_damage_events`/the combined query
string all removed) rather than debug further with production broken.

**If this is revisited:** reproduce under real thread-pool concurrency (not
just a single isolated call) before trusting it again — the mocked tests and
the single-call live test both looked clean precisely because they didn't
exercise the concurrent-multi-report path where the regression actually
showed up. Consider testing with the `@include` directive removed (send
two full separate aliased fields unconditionally, no boolean toggling) as a
simpler variant that still gets the "one HTTP request" win without the
conditional-field mechanism, in case that's implicated.

## Also noted, not a performance item

`calendar_fetcher._fetch_reports_for_guild_raw()` hardcodes `limit: 100`
on the guild report-list query with no pagination loop. A guild with 100+
reports in the requested window (very active guild, or a long date range
without an early first-kill cutoff) would have its report list silently
truncated — no error, no warning, just missing data. Doesn't cost points
either way, but worth fixing for correctness before leaning harder on wide
date ranges. Fix: paginate the same way `_fetch_death_events`/
`_fetch_damage_taken_events` already do (loop on a `nextPageTimestamp`-style
cursor, or WCL's report-list pagination equivalent — check the schema for
whether `reports()` exposes one).

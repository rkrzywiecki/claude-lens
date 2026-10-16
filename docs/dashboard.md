# Dashboard

```bash
uv run claude-lens dashboard              # http://127.0.0.1:8789
uv run claude-lens dashboard --port 9000 --db examples/demo.db
```

A single local page over the same queries as the CLI. Opening it imports changed transcripts
(the **Import new transcripts** button does it again).

- **Filters**, one row at the top: date range (7, 30, 90 days, all) and project. They scope
  everything below.
- **Totals:** API-equivalent cost, requests, cache hit ratio, cost saved by caching, and the
  avoidable cache loss of the sessions the cache doctor lists, and the cost per commit of
  sessions in git repositories ([outcomes.md](outcomes.md); a rough proxy, not quality).
- **Recommendations:** the `insights` findings with severity, estimated saving, documentation
  link and the sessions they apply to (click one to open it), the best-practice scorecard, and
  **Ask Claude for a plan** with a language picker (calls the coach; see
  [insights.md](insights.md#coach)).
- **Trends:** one small line chart per measure over the last eight local ISO weeks (cost,
  context cost, long sessions without reset, multi-file changes planned, sessions with rework,
  cache hit ratio, avoidable cache penalty, thinking share). Each chart has its own single y
  axis (shares 0-100%), the latest value and its change against the week before (arrow for the
  direction, word and color for better or worse). Weeks with nothing to measure leave a gap;
  the running week is dashed. Hover or focus a chart and use the arrow keys for each week with
  the counts behind a share. **Show table** lists every measure and the cost share by model and
  effort. Trends follow the project filter but not the date range. See [trends.md](trends.md).
- **Cost per day**, stacked by token kind (cache read, cache write, output, input); days without
  requests are shown empty. **Show table** lists the same numbers.
- **Cost by model** and **cost by project** (top seven, the rest folded into "Other").
- **Cache doctor:** advice and the sessions with misses. Click a row to open the session.
- **Possible agent loops.** Click a row to open the session.
- **Session:** prompt tokens per request with cache misses (triangles) and context resets
  (diamonds), and the request timeline. Hover the chart, or focus it and use the arrow keys.
  Its facts include commits, files, changed lines and cost per commit when the session ran in
  a git repository.

The page follows the system light or dark mode and works at phone width. It loads no external
scripts, fonts or styles.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/summary?days=&project=` | Reports by day, model and project, the cache doctor (top 10 sessions), loops, the list of projects, and `outcomes` (git outcome by main model) |
| `GET /api/sessions/{id}` | `SessionDetail` for a full id or unique prefix; 404 otherwise |
| `GET /api/insights?days=&project=` | `InsightsReport`: profile, recommendations, practices |
| `GET /api/trends?weeks=8&project=` | `TrendsReport`: metric definitions, one entry per local ISO week (oldest first), last week's changes; `weeks` 1-104 |
| `POST /api/coach?days=&project=&lang=` | The coach's plan (`CoachResult`); 503 with the reason when it cannot run |
| `POST /api/import` | Import statistics; also reads git outcomes unless `LENS_GIT=false` or `dashboard --no-git` |
| `GET /healthz` | `{"status": "ok"}` |

Paths under the home directory are shown as `~/...`; the project filter accepts that form.

## Security

The dashboard has no authentication. It listens on `127.0.0.1` unless `--allow-non-loopback` is
given, and it answers only requests whose `Host` header is a loopback name (`127.0.0.1`,
`localhost`, `[::1]`), so another web page cannot read it through DNS rebinding. `POST`
requests also need the header `x-lens-request: 1`: a page on another site cannot add a custom
header without a CORS preflight, which the dashboard never approves, so it cannot start an
import or a paid coach call. All text from
the database reaches the page through `textContent`, never as HTML.

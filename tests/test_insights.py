"""Insights: data capture (effort, thinking, session metadata), rules, practices, what-if, coach."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from conftest import assistant, write_jsonl
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from claude_lens.analysis.cache import diagnose
from claude_lens.cli import app as cli_app
from claude_lens.config import Settings
from claude_lens.importers.claude_code import import_transcripts, parse_transcript_data
from claude_lens.insights import rules
from claude_lens.insights.coach import CoachError, coach_payload, run_coach
from claude_lens.insights.engine import build_insights
from claude_lens.insights.models import InsightsReport
from claude_lens.insights.practices import practices
from claude_lens.insights.stats import build_session_stats
from claude_lens.insights.whatif import reprice
from claude_lens.models import RequestFeatures, RequestRecord, SessionMeta, ToolCall, Usage
from claude_lens.pricing import Pricing, load_pricing
from claude_lens.proxy.recorder import request_effort, request_features
from claude_lens.queries import Filters
from claude_lens.store import SCHEMA_VERSION, Store
from claude_lens.web.app import create_dashboard

T0 = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
runner = CliRunner()


@pytest.fixture
def pricing() -> Pricing:
    return load_pricing()


def rec(
    n: int,
    *,
    session: str = "s1",
    model: str = "claude-opus-5-5",
    at: float = 0,
    prompt: int = 20_000,
    output: int = 500,
    thinking: int | None = None,
    effort: str | None = "medium",
    sidechain: bool = False,
    source: str = "claude_code",
    tools: list[str] | None = None,
    features: RequestFeatures | None = None,
    project: str = "/work/app",
) -> RequestRecord:
    return RequestRecord(
        message_id=f"{session}-m{n}",
        session_id=session,
        project=project,
        model=model,
        timestamp=T0 + timedelta(seconds=at or n * 30),
        usage=Usage(
            input_tokens=10,
            cache_read_tokens=prompt - 1_010,
            cache_write_5m_tokens=1_000,
            output_tokens=output,
        ),
        thinking_tokens=thinking,
        effort=effort,
        is_sidechain=sidechain,
        source=source,  # type: ignore[arg-type]
        tool_names=tools or [],
        features=features,
    )


def call(n: int, name: str, *, session: str = "s1", target: str | None = None) -> ToolCall:
    return ToolCall(
        tool_use_id=f"{session}-t{n}",
        message_id=f"{session}-m{n}",
        session_id=session,
        timestamp=T0 + timedelta(seconds=n * 30),
        tool_name=name,
        input_hash=f"h{n}",
        target=target,
        is_error=False,
    )


def context(
    records: list[RequestRecord],
    calls: list[ToolCall],
    pricing: Pricing,
    metas: dict[str, SessionMeta] | None = None,
    check_files: bool = False,
) -> rules.RuleContext:
    sessions = build_session_stats(records, calls, metas or {}, [], pricing)
    return rules.RuleContext(
        sessions=sessions,
        records=records,
        cache=diagnose(records, pricing),
        pricing=pricing,
        total_cost=sum(s.cost for s in sessions),
        check_files=check_files,
    )


# -- data capture -------------------------------------------------------------------


def _user(session: str = "sess-1", **extra: Any) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": session,
        "entrypoint": "cli",
        "version": "2.1.296",
        "message": {"role": "user", "content": "do it"},
        **extra,
    }


def test_transcript_effort_thinking_and_session_meta(tmp_path: Path) -> None:
    entry = assistant("m1", output_tokens=900, output_tokens_details={"thinking_tokens": 600})
    entry["effort"] = "high"
    entry["agentId"] = "a1"
    entry["attributionSkill"] = "dataviz"
    error = assistant("m2")
    error["isApiErrorMessage"] = True
    path = write_jsonl(
        tmp_path / "s.jsonl",
        [
            _user(permissionMode="plan", origin={"kind": "human"}),
            _user(permissionMode="auto", origin={"kind": "human"}, interruptedMessageId="x"),
            _user(origin={"kind": "task-notification"}),
            _user(
                toolDenialKind="user",
                message={
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "t", "is_error": True}],
                },
            ),
            _user(isMeta=True, message={"role": "user", "content": "<system-reminder>"}),
            entry,
            error,
            {
                "type": "system",
                "subtype": "turn_duration",
                "sessionId": "sess-1",
                "durationMs": 5000,
            },
        ],
    )
    data = parse_transcript_data(path)
    record = next(r for r in data.records if r.message_id == "m1")
    assert (record.effort, record.thinking_tokens, record.agent_id) == ("high", 600, "a1")
    [meta] = data.sessions
    assert meta.entrypoint == "cli"
    assert meta.permission_modes == ["plan", "auto"]
    assert meta.human_prompts == 2
    assert (meta.interruptions, meta.tool_denials, meta.api_errors) == (1, 1, 1)
    assert meta.skills == ["dataviz"]
    assert meta.active_ms == 5000


def test_session_meta_is_summed_per_file_and_idempotent(tmp_path: Path, store: Store) -> None:
    root = tmp_path / "projects"
    write_jsonl(root / "p" / "a.jsonl", [_user(origin={"kind": "human"}), assistant("m1")])
    write_jsonl(
        root / "p" / "sess-1" / "subagents" / "x.jsonl",
        [_user(origin={"kind": "human"}, permissionMode="auto")],
    )
    import_transcripts(store, root)
    import_transcripts(store, root, full=True)
    meta = store.session_meta()["sess-1"]
    assert meta.human_prompts == 2
    assert "auto" in meta.permission_modes


def test_proxy_request_features() -> None:
    body = {
        "stream": True,
        "max_tokens": 64_000,
        "tools": [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral"}}],
        "thinking": {"type": "adaptive"},
        "system": [{"type": "text", "text": "x" * 120}],
        "output_config": {"effort": "low"},
        "messages": [],
    }
    features = request_features(body)
    assert features == RequestFeatures(
        stream=True,
        max_tokens=64_000,
        tool_count=2,
        cache_control=True,
        thinking="adaptive",
        system_chars=120,
    )
    assert request_effort(body) == "low"
    assert request_features({"system": "abc"}).system_chars == 3
    assert not request_features({"messages": [{"content": "hi"}]}).cache_control


def test_migrates_v3_database(tmp_path: Path) -> None:
    path = tmp_path / "v3.db"
    with Store(path) as store:
        store.upsert([rec(1)])
    conn = sqlite3.connect(path)
    conn.executescript(
        "ALTER TABLE requests DROP COLUMN effort; ALTER TABLE requests DROP COLUMN thinking_tokens;"
        "ALTER TABLE requests DROP COLUMN agent_id; ALTER TABLE requests DROP COLUMN features;"
        "DROP TABLE session_meta; PRAGMA user_version = 3;"
    )
    conn.close()
    with Store(path) as store:
        [record] = store.requests()
        assert record.effort is None
        assert store.session_meta() == {}
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
    conn.close()


# -- rules ------------------------------------------------------------------------------


def test_big_model_light_sessions_reprices_on_sonnet(pricing: Pricing) -> None:
    records = [rec(i, tools=["Read"]) for i in range(4)]
    calls = [call(i, "Read") for i in range(4)]
    [found] = rules.big_model_light_sessions(context(records, calls, pricing))
    assert found.savings_kind == "reprice"
    expected = reprice(records, pricing, rules.CODING_ALTERNATIVE).savings
    assert found.savings_usd == pytest.approx(expected, abs=0.01)


def test_sessions_with_edits_are_not_light(pricing: Pricing) -> None:
    records = [rec(i) for i in range(4)]
    calls = [call(0, "Edit", target="/a.py")]
    assert rules.big_model_light_sessions(context(records, calls, pricing)) == []


def test_big_model_subagents(pricing: Pricing) -> None:
    records = [rec(0)] + [rec(i, sidechain=True, prompt=60_000) for i in range(1, 6)]
    [found] = rules.big_model_subagents(context(records, [], pricing))
    assert found.savings_kind == "upper_bound"
    assert found.savings_usd and found.savings_usd > 0


def test_high_effort_light_work(pricing: Pricing) -> None:
    records = [rec(i, effort="high", output=2_000, thinking=1_500) for i in range(4)]
    [found] = rules.high_effort_light_work(context(records, [], pricing))
    thinking_cost = 4 * 1_500 * 20.0 / 1e6
    assert found.savings_usd == pytest.approx(thinking_cost * rules.EFFORT_THINKING_REDUCTION)
    assert found.confidence == "low"


def test_kitchen_sink_needs_prompts_size_and_no_reset(pricing: Pricing) -> None:
    records = [rec(i, prompt=60_000 + i * 20_000) for i in range(8)]
    meta = {"s1": SessionMeta(session_id="s1", source_file="f", human_prompts=9)}
    [found] = rules.kitchen_sink_sessions(context(records, [], pricing, meta))
    assert found.savings_kind == "upper_bound"
    few = {"s1": SessionMeta(session_id="s1", source_file="f", human_prompts=3)}
    assert rules.kitchen_sink_sessions(context(records, [], pricing, few)) == []


def test_exploration_and_plan_rules(pricing: Pricing) -> None:
    records = [rec(i) for i in range(32)]
    reads = [call(i, "Grep") for i in range(26)]
    edits = [call(26 + i, "Edit", target=f"/f{i}.py") for i in range(5)]
    ctx = context(records, reads + edits, pricing)
    assert [i.id for i in rules.exploration_without_subagents(ctx)] == [
        "workflow.exploration_in_main_context"
    ]
    assert [i.id for i in rules.large_changes_without_plan(ctx)] == [
        "workflow.no_plan_for_large_changes"
    ]
    planned = context(records, [*reads, *edits, call(31, "ExitPlanMode")], pricing)
    assert rules.large_changes_without_plan(planned) == []
    delegated = context(records, [*reads, call(31, "Agent")], pricing)
    assert rules.exploration_without_subagents(delegated) == []


def test_claude_md_checks(
    tmp_path: Path, pricing: Pricing, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rules, "TEMP_ROOTS", ())  # tmp_path itself is under a temp root
    missing, long_md = tmp_path / "missing", tmp_path / "long"
    missing.mkdir()
    (long_md / ".claude").mkdir(parents=True)
    (long_md / ".claude" / "CLAUDE.md").write_text("line\n" * 250)
    records = [
        rec(i, session=f"{p.name}{i}", project=str(p)) for p in (missing, long_md) for i in range(3)
    ]
    found = {i.id: i for i in rules.claude_md(context(records, [], pricing, check_files=True))}
    assert set(found) == {"setup.claude_md_missing", "setup.claude_md_too_long"}
    assert "250 lines" in found["setup.claude_md_too_long"].evidence[0]
    assert rules.claude_md(context(records, [], pricing, check_files=False)) == []


def test_temporary_directories_are_not_projects() -> None:
    assert not rules.is_project_dir(Path("/private/tmp/x"))
    assert not rules.is_project_dir(Path("relative"))


def test_api_rules(pricing: Pricing) -> None:
    plain = RequestFeatures(stream=False, max_tokens=32_000, tool_count=0)
    records = [
        RequestRecord(
            message_id=f"a{i}",
            session_id="api",
            project="/svc",
            model="claude-opus-5-5",
            timestamp=T0 + timedelta(seconds=i),
            usage=Usage(input_tokens=10_000, output_tokens=100),
            source="proxy",
            effort="high",
            thinking_tokens=80,
            features=plain,
        )
        for i in range(60)
    ]
    ctx = context(records, [], pricing)
    ids = {i.id for rule in rules.RULES for i in rule(ctx)}
    assert {
        "api.no_prompt_caching",
        "api.small_outputs_on_big_model",
        "api.high_effort_short_answers",
        "api.non_streaming_large_max_tokens",
        "api.batch_candidates",
    } <= ids


def test_severity_scale() -> None:
    assert rules.severity(None, 100) == "info"
    assert rules.severity(6, 1000) == "high"
    assert rules.severity(0.6, 10) == "high"
    assert rules.severity(1.5, 1000) == "medium"
    assert rules.severity(0.2, 1000) == "low"


def test_practices_statuses(pricing: Pricing) -> None:
    records = [rec(i, tools=["Read"]) for i in range(4)]
    status = {p.id: p for p in practices(context(records, [], pricing))}
    assert status["match_model_to_task"].status == "fail"
    assert status["api_streaming"].status == "n/a"
    assert status["api_streaming"].evidence.startswith("no API requests")


def test_build_insights_end_to_end(tmp_path: Path, pricing: Pricing) -> None:
    with Store(tmp_path / "lens.db") as store:
        store.upsert([rec(i, tools=["Read"]) for i in range(4)])
        report = build_insights(store, pricing, Filters(), check_files=False)
    assert report.profile.requests == 4
    assert report.profile.models[0].key == "claude-opus-5-5"
    assert report.insights[0].id == "model.big_model_light_sessions"
    assert report.total_savings_usd == pytest.approx(report.insights[0].savings_usd or 0)


def test_whatif_skips_same_model_and_rejects_unknown(pricing: Pricing) -> None:
    records = [rec(0), rec(1, model="claude-sonnet-5-5")]
    result = reprice(records, pricing, "claude-sonnet-5-5")
    assert result.requests == 1
    with pytest.raises(ValueError, match="no price"):
        reprice(records, pricing, "gpt-x")


# -- coach -------------------------------------------------------------------------------


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.request: dict[str, Any] = {}

    def create(self, **request: Any) -> Any:
        self.request = request
        if self.error:
            raise self.error
        return self.response


def fake_client(messages: FakeMessages, credentials: bool = True) -> Any:
    return SimpleNamespace(
        api_key="test" if credentials else None,
        auth_token=None,
        credentials=None,
        beta=SimpleNamespace(messages=messages),
    )


def message(text: str = "## Plan\n- do it", stop: str = "end_turn") -> Any:
    return SimpleNamespace(
        model="claude-opus-5-5",
        stop_reason=stop,
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        usage=SimpleNamespace(
            input_tokens=2_000,
            output_tokens=500,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )


def sample_report(pricing: Pricing) -> InsightsReport:
    with Store(Path(":memory:")) as store:
        store.upsert(
            [rec(i, tools=["Read"], project="/Users/someone/secret-project") for i in range(4)]
        )
        return build_insights(store, pricing, Filters(), check_files=False)


def test_coach_payload_has_no_paths_or_ids(pricing: Pricing) -> None:
    report = sample_report(pricing)
    text = json.dumps(coach_payload(report, "Polish"))
    assert "secret-project" not in text
    assert "s1-m" not in text and '"s1"' not in text
    assert '"language": "Polish"' in text


def test_coach_calls_api_with_fallbacks(pricing: Pricing) -> None:
    messages = FakeMessages(message())
    result = run_coach(
        sample_report(pricing), pricing, "claude-opus-5-5", client=fake_client(messages)
    )
    assert result.text == "## Plan\n- do it"
    assert result.cost == pytest.approx(2_000 * 4 / 1e6 + 500 * 20 / 1e6)
    assert messages.request["fallbacks"] == "default"
    assert messages.request["betas"] == ["server-side-fallback-2026-07-01"]
    assert messages.request["output_config"] == {"effort": "medium"}


def test_coach_without_fallbacks_for_haiku(pricing: Pricing) -> None:
    messages = FakeMessages(message())
    run_coach(sample_report(pricing), pricing, "claude-haiku-5-5", client=fake_client(messages))
    assert "fallbacks" not in messages.request


def test_coach_errors(pricing: Pricing) -> None:
    report = sample_report(pricing)
    with pytest.raises(CoachError, match="credentials"):
        run_coach(
            report,
            pricing,
            "claude-opus-5-5",
            client=fake_client(FakeMessages(), False),
            backend="api",
        )
    with pytest.raises(CoachError, match="declined"):
        run_coach(
            report,
            pricing,
            "claude-opus-5-5",
            client=fake_client(FakeMessages(message(stop="refusal"))),
        )
    with pytest.raises(CoachError, match="no text"):
        run_coach(
            report, pricing, "claude-opus-5-5", client=fake_client(FakeMessages(message(text=" ")))
        )
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    with pytest.raises(CoachError, match="reach"):
        run_coach(
            report,
            pricing,
            "claude-opus-5-5",
            client=fake_client(FakeMessages(error=anthropic.APIConnectionError(request=request))),
        )


# -- CLI and dashboard --------------------------------------------------------------------


def _seed(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "projects" / "-work-app" / "sess-1.jsonl",
        [assistant(f"m{i}", ts=f"2026-10-12T10:0{i}:00Z", output_tokens=400) for i in range(5)],
    )


def test_cli_insights_and_whatif(tmp_path: Path) -> None:
    _seed(tmp_path)
    result = runner.invoke(cli_app, ["insights", "--json", "--no-check-files"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["profile"]["requests"] == 5
    assert data["coach"] is None

    result = runner.invoke(cli_app, ["insights"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "Best practices" in result.output

    dry = runner.invoke(cli_app, ["insights", "--coach", "--dry-run", "--lang", "Polish"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.output)["language"] == "Polish"

    result = runner.invoke(cli_app, ["whatif", "--to", "claude-sonnet-5-5", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["requests"] == 5
    assert runner.invoke(cli_app, ["whatif", "--to", "claude-sonnet-5-5"]).exit_code == 0
    assert runner.invoke(cli_app, ["whatif", "--to", "nope"]).exit_code == 2
    assert (
        runner.invoke(cli_app, ["whatif", "--to", "claude-sonnet-5-5", "--part", "x"]).exit_code
        == 2
    )


def test_cli_coach_without_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed(tmp_path)
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # no credentials profile on disk
    result = runner.invoke(cli_app, ["insights", "--coach"])
    assert result.exit_code == 2
    assert "credentials" in result.output


def test_dashboard_insights_and_coach(tmp_path: Path, pricing: Pricing) -> None:
    _seed(tmp_path)
    db = tmp_path / "lens.db"
    settings = Settings(db_path=db, transcripts_dir=tmp_path / "projects")
    messages = FakeMessages(message())
    app = create_dashboard(
        settings,
        open_store=lambda: Store(db),
        pricing=pricing,
        coach_client=cast(Any, fake_client(messages)),
    )
    client = TestClient(app, base_url="http://127.0.0.1")
    assert client.post("/api/import").status_code == 403  # action header required
    client.post("/api/import", headers={"x-lens-request": "1"})
    report = client.get("/api/insights").json()
    assert report["profile"]["requests"] == 5
    assert client.post("/api/coach").status_code == 403
    coach = client.post(
        "/api/coach", params={"lang": "Polish"}, headers={"x-lens-request": "1"}
    ).json()
    assert coach["text"].startswith("## Plan")
    assert "Polish" in messages.request["messages"][0]["content"]

    failing = create_dashboard(
        settings.model_copy(update={"coach_backend": "api"}),
        open_store=lambda: Store(db),
        pricing=pricing,
        coach_client=cast(Any, fake_client(FakeMessages(), credentials=False)),
    )
    response = TestClient(failing, base_url="http://127.0.0.1").post(
        "/api/coach", headers={"x-lens-request": "1"}
    )
    assert response.status_code == 503


# -- coach through the Claude Code CLI -----------------------------------------------------

FAKE_CLAUDE = """#!{python}
import json, os, sys
args = sys.argv[1:]
prompt = sys.stdin.read()
with open({log!r}, "w") as fh:
    json.dump({{"args": args, "cwd": os.getcwd(), "prompt": prompt}}, fh)
mode = {mode!r}
if mode == "ok":
    print(json.dumps({{
        "type": "result", "subtype": "success", "is_error": False,
        "result": "## Plan\\n- clear more often",
        "total_cost_usd": 0.0123,
        "usage": {{"input_tokens": 3000, "output_tokens": 400,
                   "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
        "modelUsage": {{"claude-opus-5-5": {{}}}},
    }}))
elif mode == "error":
    print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True,
                      "result": "Not logged in"}}))
    sys.exit(1)
else:
    print("boom", file=sys.stderr)
    sys.exit(2)
"""


def fake_cli(tmp_path: Path, mode: str) -> tuple[str, Path]:
    import sys

    log = tmp_path / f"cli-{mode}.json"
    script = tmp_path / f"claude-{mode}"
    script.write_text(FAKE_CLAUDE.format(python=sys.executable, log=str(log), mode=mode))
    script.chmod(0o755)
    return str(script), log


def test_coach_through_claude_code(tmp_path: Path, pricing: Pricing) -> None:
    claude_bin, log = fake_cli(tmp_path, "ok")
    no_key = cast(Any, fake_client(FakeMessages(), credentials=False))
    result = run_coach(
        sample_report(pricing),
        pricing,
        "claude-opus-5-5",
        "low",
        "Polish",
        client=no_key,
        claude_bin=claude_bin,
    )
    assert (result.backend, result.model, result.cost) == ("claude-code", "claude-opus-5-5", 0.0123)
    assert result.text.startswith("## Plan")
    assert result.usage.output_tokens == 400
    call = json.loads(log.read_text())
    args = call["args"]
    assert args[:3] == ["-p", "--output-format", "json"]
    assert args[args.index("--tools") + 1] == ""
    assert args[args.index("--model") + 1] == "claude-opus-5-5"
    assert args[args.index("--effort") + 1] == "low"
    assert "--no-session-persistence" in args
    assert "--system-prompt" in args
    assert "Write the report in Polish" in call["prompt"]
    assert "claude-lens-coach-" in call["cwd"]  # an empty temporary directory


def test_coach_prefers_api_when_credentials_exist(tmp_path: Path, pricing: Pricing) -> None:
    claude_bin, log = fake_cli(tmp_path, "ok")
    messages = FakeMessages(message())
    result = run_coach(
        sample_report(pricing),
        pricing,
        "claude-opus-5-5",
        client=cast(Any, fake_client(messages)),
        claude_bin=claude_bin,
    )
    assert result.backend == "api"
    assert not log.exists()


def test_coach_claude_code_errors(tmp_path: Path, pricing: Pricing) -> None:
    report = sample_report(pricing)
    for mode, match in (("error", "Not logged in"), ("crash", "boom")):
        claude_bin, _ = fake_cli(tmp_path, mode)
        with pytest.raises(CoachError, match=match):
            run_coach(
                report, pricing, "claude-opus-5-5", backend="claude-code", claude_bin=claude_bin
            )
    with pytest.raises(CoachError, match="not found"):
        run_coach(
            report, pricing, "claude-opus-5-5", backend="claude-code", claude_bin="/no/such/cli"
        )
    no_key = cast(Any, fake_client(FakeMessages(), credentials=False))
    with pytest.raises(CoachError, match=r"no Claude API credentials .* and no"):
        run_coach(report, pricing, "claude-opus-5-5", client=no_key, claude_bin="no-such-cli-x")

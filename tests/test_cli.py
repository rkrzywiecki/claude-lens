import json
from pathlib import Path

from conftest import assistant, write_jsonl
from typer.testing import CliRunner

from claude_lens.cli import app

runner = CliRunner()


def _transcripts(tmp_path: Path) -> None:
    write_jsonl(
        tmp_path / "projects" / "-work-app" / "s1.jsonl",
        [
            assistant("m1", ts="2026-10-12T10:00:00Z", output_tokens=1_000_000),
            assistant("m2", ts="2026-10-13T10:00:00Z", model="claude-sonnet-5-5"),
        ],
    )


def test_import_then_report_json(tmp_path: Path) -> None:
    _transcripts(tmp_path)
    result = runner.invoke(app, ["import"])
    assert result.exit_code == 0, result.output
    assert "2 responses" in result.output

    result = runner.invoke(app, ["report", "--by", "model", "--json", "--no-refresh"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["group_by"] == "model"
    assert data["total"]["requests"] == 2
    assert {r["key"] for r in data["rows"]} == {"claude-opus-5-5", "claude-sonnet-5-5"}


def test_report_refreshes_by_default(tmp_path: Path) -> None:
    _transcripts(tmp_path)
    result = runner.invoke(app, ["report", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["total"]["requests"] == 2


def test_report_date_range(tmp_path: Path) -> None:
    _transcripts(tmp_path)
    result = runner.invoke(
        app, ["report", "--json", "--since", "2026-10-13", "--until", "2026-10-13"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["total"]["requests"] == 1


def test_report_table_output(tmp_path: Path) -> None:
    _transcripts(tmp_path)
    result = runner.invoke(app, ["report", "--by", "project"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "-work-app" not in result.output  # records carry a cwd
    assert "/work/app" in result.output
    assert "Total" in result.output


def test_report_empty_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report"])
    assert result.exit_code == 0, result.output
    assert "No requests" in result.output


def test_invalid_options(tmp_path: Path) -> None:
    assert runner.invoke(app, ["report", "--by", "week"]).exit_code == 2
    assert runner.invoke(app, ["report", "--days", "3", "--since", "2026-10-01"]).exit_code == 2


def test_import_missing_directory(tmp_path: Path) -> None:
    result = runner.invoke(app, ["import", "--dir", str(tmp_path / "nope")])
    assert result.exit_code == 2


def test_bad_pricing_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["report"], env={"LENS_PRICING_PATH": str(tmp_path / "x.yaml")})
    assert result.exit_code == 2


def test_money_and_tokens_format() -> None:
    from claude_lens.render import money, tokens

    assert money(1.4) == "$1.40"
    assert money(-0.186) == "-$0.19"
    assert money(-0.001) == "$0.00"
    assert [tokens(n) for n in (950, 12_300, 4_500_000, 1_200_000_000)] == [
        "950",
        "12.3k",
        "4.5M",
        "1.2B",
    ]

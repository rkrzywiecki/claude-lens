from pathlib import Path

import pytest
from conftest import assistant, write_jsonl
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from claude_lens.cli import app as cli_app
from claude_lens.config import Settings
from claude_lens.store import Store
from claude_lens.web.app import create_dashboard

ACTION = {"x-lens-request": "1"}


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    root = tmp_path / "projects"
    home_project = str(Path.home() / "work" / "app")
    write_jsonl(
        root / "-work-app" / "sess-1.jsonl",
        [
            assistant("m1", ts="2026-10-12T10:00:00Z", cwd=home_project, output_tokens=1_000),
            assistant(
                "m2",
                ts="2026-10-12T10:20:00Z",
                cwd=home_project,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=40_000,
            ),
            assistant("m3", ts="2026-10-12T11:00:00Z", session="sess-2", cwd="/other"),
        ],
    )
    db = tmp_path / "lens.db"
    settings = Settings(db_path=db, transcripts_dir=root)
    return TestClient(
        create_dashboard(settings, open_store=lambda: Store(db)), base_url="http://127.0.0.1"
    )


def test_page_and_import(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "<title>claude-lens</title>" in page.text
    stats = client.post("/api/import", headers=ACTION).json()
    assert stats["records"] == 3


def test_summary(client: TestClient) -> None:
    client.post("/api/import", headers=ACTION)
    data = client.get("/api/summary").json()
    assert data["by_day"]["total"]["requests"] == 3
    assert {"by_model", "by_project", "cache", "loops", "projects"} <= data.keys()
    assert "~/work/app" in data["projects"]
    assert data["cache"]["sessions"][0]["project"] == "~/work/app"  # shortened for display

    only = client.get("/api/summary", params={"project": "~/work/app"}).json()
    assert only["by_day"]["total"]["requests"] == 2
    assert client.get("/api/summary", params={"days": 0}).status_code == 422


def test_session(client: TestClient) -> None:
    client.post("/api/import", headers=ACTION)
    detail = client.get("/api/sessions/sess-1").json()
    assert len(detail["timeline"]) == 2
    assert detail["project"] == "~/work/app"
    assert client.get("/api/sessions/nope").status_code == 404


def test_foreign_host_is_rejected(client: TestClient) -> None:
    assert client.get("/healthz").status_code == 200
    assert client.get("/healthz", headers={"host": "evil.example"}).status_code == 400


def test_import_without_transcripts(tmp_path: Path) -> None:
    db = tmp_path / "lens.db"
    settings = Settings(db_path=db, transcripts_dir=tmp_path / "missing")
    client = TestClient(
        create_dashboard(settings, open_store=lambda: Store(db)), base_url="http://localhost"
    )
    assert client.post("/api/import", headers=ACTION).json()["records"] == 0


def test_cli_refuses_non_loopback_dashboard() -> None:
    result = CliRunner().invoke(cli_app, ["dashboard", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2
    assert "non-loopback" in result.output

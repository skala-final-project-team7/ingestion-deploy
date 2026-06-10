"""JsonFixtureSourceAdapter 픽스처 로딩 회귀 — 부재 파일의 명확한 오류 표면화 (P1-6).

종전에는 기본 설정의 ``POST /ml/ingest`` 가 픽스처 부재 시 원인 불명 ``FileNotFoundError``
로 잡 FAILED 가 됐다. ``_iter_raw_pages`` 가 경로를 포함한 메시지로 즉시 실패하는지와,
파일이 존재하면 기존대로 순회하는지를 검증한다(매핑 상세는 crawl/pipeline 테스트가 커버).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters.json_fixture import JsonFixtureSourceAdapter


def test_missing_fixture_raises_file_not_found_with_path(tmp_path: Path) -> None:
    adapter = JsonFixtureSourceAdapter(samples_dir=tmp_path, fixture_files=["missing.json"])

    with pytest.raises(FileNotFoundError) as excinfo:
        list(adapter.fetch_pages())

    message = str(excinfo.value)
    # 오류 메시지에 실제 탐색 경로와 설정 힌트(RAG_SAMPLES_DIR)가 포함돼야 한다(P1-6).
    assert str(tmp_path / "missing.json") in message
    assert "RAG_SAMPLES_DIR" in message


def test_existing_fixture_iterates_without_error(tmp_path: Path) -> None:
    (tmp_path / "ok.json").write_text(
        json.dumps({"single_page_responses": []}), encoding="utf-8"
    )
    adapter = JsonFixtureSourceAdapter(samples_dir=tmp_path, fixture_files=["ok.json"])

    assert list(adapter.fetch_pages()) == []

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from detect_gh_aw import (  # noqa: E402
    GitHubClient,
    RepositoryRef,
    detect_matching_workflow_names,
    extract_repository_references,
    parse_repository_reference,
    resolve_repository_source,
)


def test_exact_md_and_lock_pair_is_detected() -> None:
    files = [
        {"name": "daily-report.md", "type": "file"},
        {"name": "daily-report.lock.yml", "type": "file"},
    ]

    assert detect_matching_workflow_names(files) == ["daily-report"]


def test_different_base_names_are_not_detected() -> None:
    files = [
        {"name": "daily-report.md", "type": "file"},
        {"name": "weekly-report.lock.yml", "type": "file"},
    ]

    assert detect_matching_workflow_names(files) == []


def test_near_miss_cases_are_not_detected() -> None:
    assert detect_matching_workflow_names(
        [
            {"name": "agent.md", "type": "file"},
            {"name": "agent.yml", "type": "file"},
        ]
    ) == []
    assert detect_matching_workflow_names(
        [{"name": "agent.lock.yml", "type": "file"}]
    ) == []
    assert detect_matching_workflow_names(
        [
            {"name": "agent.md", "type": "file"},
            {"name": "agent.lock.yml", "type": "dir"},
        ]
    ) == []


def test_repository_reference_parser_accepts_common_forms() -> None:
    assert parse_repository_reference("owner/repository").full_name == "owner/repository"
    assert (
        parse_repository_reference("https://github.com/owner/repository.git").full_name
        == "owner/repository"
    )
    assert (
        parse_repository_reference("git@github.com:owner/repository.git").full_name
        == "owner/repository"
    )
    assert (
        parse_repository_reference("https://api.github.com/repos/owner/repository").full_name
        == "owner/repository"
    )
    assert parse_repository_reference("https://gitlab.com/owner/repository") is None


def test_owner_and_repository_columns_are_combined() -> None:
    frame = pd.DataFrame(
        {
            "owner": ["first-owner", "second-owner"],
            "repository_name": ["first-repo", "second-repo"],
            "language": ["Python", "TypeScript"],
        }
    )

    source = resolve_repository_source(frame)
    references = extract_repository_references(frame, source)

    assert source.mode == "pair"
    assert [reference.full_name for reference in references if reference] == [
        "first-owner/first-repo",
        "second-owner/second-repo",
    ]


class FakeResponse:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers

    def json(self) -> object:
        return self._payload


def test_client_queries_only_workflows_directory_and_detects_exact_pair() -> None:
    response = FakeResponse(
        200,
        [
            {"name": "daily-report.md", "type": "file"},
            {"name": "daily-report.lock.yml", "type": "file"},
        ],
        {"X-RateLimit-Remaining": "10"},
    )
    client = GitHubClient("test-token", max_retries=0)
    calls: list[str] = []

    def fake_get(url: str, **_: object) -> FakeResponse:
        calls.append(url)
        return response

    client.session.get = fake_get  # type: ignore[method-assign]
    result = client.inspect_workflows(RepositoryRef("owner", "repo"))

    assert result.gh_aw == 1
    assert result.status == "detected"
    assert result.matches == ["daily-report"]
    assert calls == [
        "https://api.github.com/repos/owner/repo/contents/.github/workflows"
    ]
    assert client.session.headers["Authorization"] == "Bearer test-token"


def test_client_retries_primary_rate_limit_before_classifying() -> None:
    responses = iter(
        [
            FakeResponse(
                403,
                {"message": "API rate limit exceeded"},
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": "4102444800",
                },
            ),
            FakeResponse(
                200,
                [{"name": "agent.md", "type": "file"}],
                {"X-RateLimit-Remaining": "10"},
            ),
        ]
    )
    client = GitHubClient(None, max_retries=1)
    client._sleep = lambda _seconds, _reason: None  # type: ignore[method-assign]
    client.session.get = lambda _url, **_kwargs: next(responses)  # type: ignore[method-assign]

    result = client.inspect_workflows(RepositoryRef("owner", "repo"))

    assert result.gh_aw == 0
    assert result.status == "not_detected"

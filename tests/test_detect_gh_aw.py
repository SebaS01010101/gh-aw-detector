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
from process_gh_aw_graphql import GitHubGraphQLClient, build_output  # noqa: E402


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


def test_graphql_batch_detects_exact_pair_and_missing_directory() -> None:
    payload = {
        "data": {
            "rateLimit": {"remaining": 10, "cost": 1},
            "r0": {
                "name": "repo-name",
                "object": {
                    "__typename": "Tree",
                    "entries": [
                        {"name": "agent.md", "type": "blob"},
                        {"name": "agent.lock.yml", "type": "blob"},
                        {"name": "other.lock.yml", "type": "blob"},
                    ],
                },
            },
            "r1": {"name": "without-workflows", "object": None},
        }
    }
    client = GitHubGraphQLClient("test-token", max_retries=0)
    client.session.post = lambda *_args, **_kwargs: FakeResponse(  # type: ignore[method-assign]
        200, payload, {"X-RateLimit-Remaining": "10"}
    )

    results = client.inspect_batch(
        [RepositoryRef("owner", "repo-name"), RepositoryRef("owner", "without-workflows")]
    )

    assert results["owner/repo-name"].status == "detected"
    assert results["owner/repo-name"].matches == ["agent"]
    assert results["owner/without-workflows"].status == "not_detected"


def test_graphql_query_escapes_repository_names() -> None:
    query, _ = GitHubGraphQLClient._query([RepositoryRef("an-owner", "repo-name")])

    assert 'owner:"an-owner"' in query
    assert 'name:"repo-name"' in query
    assert 'expression:"HEAD:.github/workflows"' in query


def test_output_does_not_turn_technical_error_into_zero() -> None:
    frame = pd.DataFrame({"repository": ["owner/repo"]})
    result = build_output(
        frame,
        [RepositoryRef("owner", "repo")],
        {
            "owner/repo": {
                "gh_aw": 0,
                "status": "network_error",
                "matches": [],
                "error": "timeout",
            }
        },
        {},
    )

    assert result.loc[0, "gh_aw"] == ""
    assert result.loc[0, "gh_aw_status"] == "network_error"

#
# Copyright 2019 Lukas Schmelzeisen
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import json
from datetime import date
from logging import getLogger
from pathlib import Path
from typing import Optional, Sequence

import pytest
from _pytest.capture import CaptureFixture
from _pytest.monkeypatch import MonkeyPatch

from nasty.cli.main import main
from nasty.request.request import DEFAULT_BATCH_SIZE, DEFAULT_MAX_TWEETS
from nasty.request.search import DEFAULT_FILTER, DEFAULT_LANG, Search, SearchFilter
from nasty.request_executor import RequestExecutor

from .mock_context import MockContext

logger = getLogger(__name__)


REQUESTS = [
    Search("trump"),
    Search("donald trump"),
    Search("trump", since=date(2019, 3, 21), until=date(2019, 3, 22)),
    Search("trump", filter_=SearchFilter.LATEST),
    Search("trump", lang="de"),
    Search("trump", max_tweets=17, batch_size=71),
    Search("trump", max_tweets=None, batch_size=DEFAULT_BATCH_SIZE),
]


def _make_args(
    request: Search, to_executor: Optional[Path] = None, daily: bool = False
) -> Sequence[str]:
    args = ["search", "--query", request.query]
    if request.since:
        args += ["--since", request.since.strftime("%Y-%m-%d")]
    if request.until:
        args += ["--until", request.until.strftime("%Y-%m-%d")]
    if request.filter != DEFAULT_FILTER:
        args += ["--filter", request.filter.name]
    if request.lang != DEFAULT_LANG:
        args += ["--lang", request.lang]
    if request.max_tweets is None:
        args += ["--max-tweets", "-1"]
    elif request.max_tweets != DEFAULT_MAX_TWEETS:
        args += ["--max-tweets", str(request.max_tweets)]
    if request.batch_size != DEFAULT_BATCH_SIZE:
        args += ["--batch-size", str(request.batch_size)]
    if to_executor is not None:
        args += ["--to-executor", str(to_executor)]
    if daily:
        args += ["--daily"]
    return args


@pytest.mark.parametrize("request_", REQUESTS, ids=repr)
def test_correct_call(
    request_: Search, monkeypatch: MonkeyPatch, capsys: CaptureFixture
) -> None:
    mock_context: MockContext[Search] = MockContext()
    monkeypatch.setattr(Search, "request", mock_context.mock_request)

    main(_make_args(request_))

    assert mock_context.request == request_
    assert not mock_context.remaining_result_tweets
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("num_results", [5, 10, 20], ids=repr)
def test_correct_call_results(
    num_results: int, monkeypatch: MonkeyPatch, capsys: CaptureFixture
) -> None:
    mock_context: MockContext[Search] = MockContext(num_results=num_results)
    monkeypatch.setattr(Search, "request", mock_context.mock_request)
    request = Search("trump", max_tweets=10)

    main(_make_args(request))

    assert mock_context.request == request
    assert not mock_context.remaining_result_tweets
    assert capsys.readouterr().out == (
        json.dumps(mock_context.RESULT_TWEET.to_json()) + "\n"
    ) * min(10, num_results)


@pytest.mark.parametrize("request_", REQUESTS, ids=repr)
def test_correct_call_to_executor(
    request_: Search, capsys: CaptureFixture, tmp_path: Path,
) -> None:
    executor_file = tmp_path / "jobs.jsonl"

    main(_make_args(request_, to_executor=executor_file))

    assert capsys.readouterr().out == ""
    request_executor = RequestExecutor()
    request_executor.load_requests(executor_file)
    assert len(request_executor._jobs) == 1
    assert request_executor._jobs[0].request == request_
    assert request_executor._jobs[0]._id
    assert request_executor._jobs[0].completed_at is None
    assert request_executor._jobs[0].exception is None


def test_correct_call_to_executor_exists(
    capsys: CaptureFixture, tmp_path: Path
) -> None:
    old_request = Search("donald")
    new_request = Search("trump")

    executor_file = tmp_path / "jobs.jsonl"
    request_executor = RequestExecutor()
    request_executor.submit(old_request)
    request_executor.dump_requests(executor_file)

    main(_make_args(new_request, to_executor=executor_file))

    assert capsys.readouterr().out == ""
    request_executor = RequestExecutor()
    request_executor.load_requests(executor_file)
    assert len(request_executor._jobs) == 2
    for i, job in enumerate(request_executor._jobs):
        assert job.request == old_request if i == 0 else new_request
        assert job._id
        assert job.completed_at is None
        assert job.exception is None


def test_correct_call_to_executor_daily(capsys: CaptureFixture, tmp_path: Path) -> None:
    executor_file = tmp_path / "jobs.jsonl"
    request = Search("trump", since=date(2019, 1, 1), until=date(2019, 2, 1))

    # Needed for type checking.
    assert request.until is not None and request.since is not None

    main(_make_args(request, to_executor=executor_file, daily=True))

    assert capsys.readouterr().out == ""
    request_executor = RequestExecutor()
    request_executor.load_requests(executor_file)
    assert len(request_executor._jobs) == (request.until - request.since).days
    for job, expected_request in zip(
        request_executor._jobs, request.to_daily_requests()
    ):
        assert job.request == expected_request
        assert job._id
        assert job.completed_at is None
        assert job.exception is None


@pytest.mark.parametrize(
    "args_string",
    [
        "",
        "trump",
        "--query trump --since 2019",
        "--query trump --until 2019",
        "--query trump --filter latest",
        "--query trump --max-tweets five",
        "--query trump --batch-size 3.0",
        "--query trump --to-executor",
        "--query trump --daily",
        "--query trump --to-executor file --daily",
        "--query trump --since 2019-03-21 --to-executor file --daily",
        "--query trump --until 2019-03-21 --to-executor file --daily",
    ],
    ids=repr,
)
def test_incorrect_call(args_string: str, capsys: CaptureFixture) -> None:
    args = ["search"]
    if args_string:
        args.extend(args_string.split(" "))
    logger.debug("Raw arguments: {}".format(args))

    with pytest.raises(SystemExit) as excinfo:
        main(args)

    assert excinfo.value.code == 2

    captured = capsys.readouterr().err
    logger.debug("Captured Error:")
    for line in captured.split("\n"):
        logger.debug("  " + line)
    assert captured.startswith("usage: nasty search")
    assert "nasty search: error" in captured

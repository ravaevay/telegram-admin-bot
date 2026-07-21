"""Tests for the Gitea Actions test stand functionality."""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
from datetime import datetime, timedelta

from modules.gitea_stands import (
    STAND_CATALOG,
    build_stand_fqdn,
    build_stand_url,
    deploy_stand,
    destroy_stand,
    dispatch_workflow,
    dispatch_and_correlate,
    get_run_status,
    list_runs,
)
from modules.database import (
    init_db,
    save_stand,
    get_stand_by_id,
    get_stands_by_creator,
    get_expiring_stands,
    get_deploying_stands,
    get_destroying_stands,
    update_stand_status,
    extend_stand_expiration,
    delete_stand,
)


class TestStandCatalog:
    def test_workflow_file_matches_service(self):
        for service, entry in STAND_CATALOG.items():
            assert entry["workflow_file"] == f"deploy-{service}.yml"

    def test_url_path_starts_with_slash(self):
        for entry in STAND_CATALOG.values():
            assert entry["url_path"].startswith("/")

    def test_inputs_shape(self):
        for entry in STAND_CATALOG.values():
            for param in entry["inputs"]:
                assert param["name"]
                assert param["label"]
                assert param["type"] in ("string", "choice")
                assert "default" in param
                if param["type"] == "choice":
                    assert param["default"] in param["options"]

    def test_build_stand_url(self):
        assert build_stand_url("wordpress", "wp-test") == f"https://{build_stand_fqdn('wp-test')}/"
        assert build_stand_url("finebi", "bi") == f"https://{build_stand_fqdn('bi')}/webroot/decision"


def _make_response(status_code=200, json_data=None):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = json_data
    response.raise_for_status = MagicMock()
    return response


def _make_client(responses_by_method):
    """Build a mock httpx.AsyncClient whose get/post return the given responses in order."""
    mock_client = AsyncMock()
    for method, responses in responses_by_method.items():
        getattr(mock_client, method).side_effect = responses
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_client
    return mock_ctx, mock_client


def _run(run_id, event="workflow_dispatch", status="completed", conclusion="success", path=None):
    run = {
        "id": run_id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://gitea.test/runs/{run_id}",
    }
    if path:
        run["path"] = path
    return run


@pytest.mark.asyncio
class TestGiteaClient:
    async def test_dispatch_payload(self):
        mock_ctx, mock_client = _make_client({"post": [_make_response(204)]})
        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await dispatch_workflow("deploy-wordpress.yml", {"mode": "deploy", "subdomain": "wp"})

        assert result["success"]
        url = mock_client.post.call_args[0][0]
        assert url.endswith("/actions/workflows/deploy-wordpress.yml/dispatches")
        payload = mock_client.post.call_args[1]["json"]
        assert payload == {"ref": "main", "inputs": {"mode": "deploy", "subdomain": "wp"}}

    async def test_list_runs_dict_format(self):
        runs = [_run(5), _run(4)]
        mock_ctx, _ = _make_client({"get": [_make_response(200, {"workflow_runs": runs, "total_count": 2})]})
        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await list_runs()
        assert result["success"]
        assert result["runs"] == runs

    async def test_list_runs_bare_list_format(self):
        runs = [_run(3)]
        mock_ctx, _ = _make_client({"get": [_make_response(200, runs)]})
        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await list_runs()
        assert result["success"]
        assert result["runs"] == runs

    async def test_correlation_picks_oldest_new_run(self):
        before = {"workflow_runs": [_run(10)]}
        after = {"workflow_runs": [_run(12), _run(11), _run(10)]}
        with (
            patch(
                "modules.gitea_stands.list_runs",
                new=AsyncMock(
                    side_effect=[
                        {"success": True, "runs": before["workflow_runs"], "message": ""},
                        {"success": True, "runs": after["workflow_runs"], "message": ""},
                    ]
                ),
            ),
            patch(
                "modules.gitea_stands.dispatch_workflow", new=AsyncMock(return_value={"success": True, "message": ""})
            ),
            patch("modules.gitea_stands.CORRELATE_INTERVAL", 0),
        ):
            result = await dispatch_and_correlate("deploy-wordpress.yml", {"mode": "deploy", "subdomain": "wp"})

        assert result["success"]
        assert result["run_id"] == 11  # oldest run newer than 10

    async def test_correlation_filters_by_workflow_path(self):
        before = [_run(10)]
        after = [
            _run(12, path=".gitea/workflows/deploy-wordpress.yml"),
            _run(11, path=".gitea/workflows/deploy-moodle.yml"),
            _run(10),
        ]
        with (
            patch(
                "modules.gitea_stands.list_runs",
                new=AsyncMock(
                    side_effect=[
                        {"success": True, "runs": before, "message": ""},
                        {"success": True, "runs": after, "message": ""},
                    ]
                ),
            ),
            patch(
                "modules.gitea_stands.dispatch_workflow", new=AsyncMock(return_value={"success": True, "message": ""})
            ),
            patch("modules.gitea_stands.CORRELATE_INTERVAL", 0),
        ):
            result = await dispatch_and_correlate("deploy-wordpress.yml", {"mode": "deploy", "subdomain": "wp"})

        assert result["run_id"] == 12

    async def test_correlation_gives_up_gracefully(self):
        empty = {"success": True, "runs": [_run(10)], "message": ""}
        with (
            patch("modules.gitea_stands.list_runs", new=AsyncMock(return_value=empty)),
            patch(
                "modules.gitea_stands.dispatch_workflow", new=AsyncMock(return_value={"success": True, "message": ""})
            ),
            patch("modules.gitea_stands.CORRELATE_INTERVAL", 0),
            patch("modules.gitea_stands.CORRELATE_ATTEMPTS", 2),
        ):
            result = await dispatch_and_correlate("deploy-wordpress.yml", {"mode": "deploy", "subdomain": "wp"})

        assert result["success"]
        assert result["run_id"] is None

    async def test_deploy_stand_inputs(self):
        with patch(
            "modules.gitea_stands.dispatch_and_correlate",
            new=AsyncMock(return_value={"success": True, "run_id": 1, "run_url": "u", "message": ""}),
        ) as mock_dc:
            await deploy_stand("moodle", "md-test", {"moodle_version": "4.5.11"})

        workflow_file, inputs = mock_dc.call_args[0]
        assert workflow_file == "deploy-moodle.yml"
        assert inputs == {"mode": "deploy", "subdomain": "md-test", "moodle_version": "4.5.11"}

    async def test_destroy_stand_inputs(self):
        with patch(
            "modules.gitea_stands.dispatch_and_correlate",
            new=AsyncMock(return_value={"success": True, "run_id": 2, "run_url": "u", "message": ""}),
        ) as mock_dc:
            await destroy_stand("wordpress", "wp-test")

        workflow_file, inputs = mock_dc.call_args[0]
        assert workflow_file == "deploy-wordpress.yml"
        assert inputs == {"mode": "destroy", "subdomain": "wp-test"}

    async def test_get_run_status(self):
        mock_ctx, _ = _make_client({"get": [_make_response(200, _run(7, status="completed", conclusion="failure"))]})
        with patch("httpx.AsyncClient", return_value=mock_ctx):
            result = await get_run_status(7)

        assert result["success"]
        assert result["status"] == "completed"
        assert result["conclusion"] == "failure"

    async def test_retry_on_500(self):
        error_response = _make_response(500)
        ok_response = _make_response(204)
        mock_ctx, mock_client = _make_client({"post": [error_response, ok_response]})
        with (
            patch("httpx.AsyncClient", return_value=mock_ctx),
            patch("modules.gitea_stands.asyncio.sleep", new=AsyncMock()),
        ):
            result = await dispatch_workflow("deploy-wordpress.yml", {"mode": "deploy", "subdomain": "wp"})

        assert result["success"]
        assert mock_client.post.call_count == 2


def _save_test_stand(**overrides):
    now = datetime.now()
    defaults = {
        "service": "wordpress",
        "subdomain": "wp-test",
        "url": "https://wp-test.onlyoffice.fun/",
        "status": "deploying",
        "deploy_run_id": 100,
        "deploy_run_url": "https://gitea.test/runs/100",
        "inputs_json": "{}",
        "creator_id": "111",
        "creator_username": "@tester",
        "expiration_date": (now + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S"),
        "created_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": "telegram",
    }
    defaults.update(overrides)
    return save_stand(**defaults)


class TestStandDatabase:
    def test_save_and_get(self, tmp_db):
        init_db()
        stand_id = _save_test_stand()
        assert stand_id is not None

        stand = get_stand_by_id(stand_id)
        assert stand["service"] == "wordpress"
        assert stand["subdomain"] == "wp-test"
        assert stand["status"] == "deploying"
        assert stand["deploy_run_id"] == 100
        assert stand["creator_id"] == "111"
        assert stand["platform"] == "telegram"
        assert stand["auto_destroy"] == 0

    def test_get_stands_by_creator(self, tmp_db):
        init_db()
        _save_test_stand(creator_id="111")
        _save_test_stand(creator_id="222", subdomain="other")
        stands = get_stands_by_creator(111)  # int lookup must match TEXT column
        assert len(stands) == 1
        assert stands[0]["subdomain"] == "wp-test"

    def test_update_status_with_destroy_run(self, tmp_db):
        init_db()
        stand_id = _save_test_stand(status="active")
        assert update_stand_status(stand_id, "destroying", destroy_run_id=200, auto_destroy=True)

        stand = get_stand_by_id(stand_id)
        assert stand["status"] == "destroying"
        assert stand["destroy_run_id"] == 200
        assert stand["auto_destroy"] == 1

    def test_status_filters_and_platform(self, tmp_db):
        init_db()
        _save_test_stand(status="deploying", platform="telegram")
        _save_test_stand(status="deploying", platform="mattermost", subdomain="mm-stand")
        _save_test_stand(status="destroying", subdomain="dying")

        assert len(get_deploying_stands()) == 2
        assert len(get_deploying_stands(platform="telegram")) == 1
        assert len(get_deploying_stands(platform="mattermost")) == 1
        assert len(get_destroying_stands()) == 1

    def test_expiring_excludes_destroying(self, tmp_db):
        init_db()
        soon = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        _save_test_stand(status="active", expiration_date=soon)
        _save_test_stand(status="destroying", expiration_date=soon, subdomain="dying")
        _save_test_stand(status="active", subdomain="fresh")  # expires in 7 days

        expiring = get_expiring_stands()
        assert len(expiring) == 1
        assert expiring[0]["status"] == "active"

    def test_extend_expiration(self, tmp_db):
        init_db()
        stand_id = _save_test_stand()
        old_exp = get_stand_by_id(stand_id)["expiration_date"]

        new_exp = extend_stand_expiration(stand_id, 3)
        assert new_exp is not None
        delta = datetime.strptime(new_exp, "%Y-%m-%d %H:%M:%S") - datetime.strptime(old_exp, "%Y-%m-%d %H:%M:%S")
        assert delta == timedelta(days=3)

    def test_delete_stand(self, tmp_db):
        init_db()
        stand_id = _save_test_stand()
        assert delete_stand(stand_id)
        assert get_stand_by_id(stand_id) is None
        assert not delete_stand(stand_id)  # already gone

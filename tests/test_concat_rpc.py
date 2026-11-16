from __future__ import annotations

import json
from pathlib import Path

import pytest

from concat_client import (
    AutomationConfig,
    ConcatAuthError,
    ConcatClient,
    ConcatConnectionError,
    ConcatExportTimeoutError,
    ConcatRPCError,
    MockConcatServer,
    load_automation_config,
    main,
)


def test_automation_config_loading_success(tmp_path: Path):
    config_file = tmp_path / "automation.json"
    config_file.write_text(
        json.dumps({"port": 49152, "token": "test-secret-token", "host": "127.0.0.1"}),
        encoding="utf-8",
    )

    cfg = load_automation_config(config_file)
    assert isinstance(cfg, AutomationConfig)
    assert cfg.port == 49152
    assert cfg.token == "test-secret-token"
    assert cfg.host == "127.0.0.1"


def test_automation_config_missing_file_raises(tmp_path: Path):
    non_existent = tmp_path / "does_not_exist.json"
    with pytest.raises(ConcatConnectionError, match="Automation config file not found"):
        load_automation_config(non_existent)


def test_automation_config_invalid_json_raises(tmp_path: Path):
    corrupt_file = tmp_path / "corrupt.json"
    corrupt_file.write_text("invalid json content {", encoding="utf-8")
    with pytest.raises(ConcatConnectionError, match="Failed to parse automation config"):
        load_automation_config(corrupt_file)


def test_automation_config_missing_required_fields_raises(tmp_path: Path):
    missing_token = tmp_path / "missing_token.json"
    missing_token.write_text(json.dumps({"port": 49152}), encoding="utf-8")
    with pytest.raises(ConcatConnectionError, match="must contain 'port' and 'token'"):
        load_automation_config(missing_token)

    missing_port = tmp_path / "missing_port.json"
    missing_port.write_text(json.dumps({"token": "abc"}), encoding="utf-8")
    with pytest.raises(ConcatConnectionError, match="must contain 'port' and 'token'"):
        load_automation_config(missing_port)


def test_mock_server_handshake_and_ping(tmp_path: Path):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        ping_resp = client.ping()
        assert ping_resp.get("status") == "ok"
        assert ping_resp.get("version") == "0.2.9"
        client.disconnect()


def test_auth_failure_with_invalid_token(tmp_path: Path):
    with MockConcatServer(token="valid-secret-token") as server:
        # Create config with wrong token
        bad_config = tmp_path / "bad_automation.json"
        bad_config.write_text(
            json.dumps({"port": server.port, "token": "wrong-token", "host": server.host}),
            encoding="utf-8",
        )

        client = ConcatClient(config_path=bad_config)
        with pytest.raises(ConcatAuthError, match="Unauthorized"):
            client.connect()


def test_jsonrpc_request_formatting_and_headers(tmp_path: Path):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        result = client.rpc_call("echo_test", {"message": "hello"})
        assert result == {"echo": "hello"}

        # Inspect server's recorded requests
        last_req = server.last_request
        assert last_req is not None
        assert last_req["headers"].get("authorization") == f"Bearer {server.token}"
        assert "application/json" in last_req["headers"].get("content-type", "")
        body = last_req["body"]
        assert body["jsonrpc"] == "2.0"
        assert body["method"] == "echo_test"
        assert body["params"] == {"message": "hello"}
        assert "id" in body
        client.disconnect()


def test_rpc_methods_execution(tmp_path: Path):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        # 1. project_create
        proj_id = client.project_create(name="test_proj")
        assert isinstance(proj_id, str)
        assert len(proj_id) > 0

        # 2. project_set_video
        set_res = client.project_set_video(
            width=1080, height=1920, fps=30, timeline_background="blur"
        )
        assert set_res.get("status") == "ok"

        # 3. media_import
        test_media = tmp_path / "narration.wav"
        test_media.write_bytes(b"dummy wav data")
        media_id = client.media_import(str(test_media))
        assert isinstance(media_id, str)
        assert len(media_id) > 0

        # 4. clip_add - test both signature styles:
        # a) clip_add(track, media_id, start_time, duration, in_point=0)
        clip_id_1 = client.clip_add("video", media_id, start_time=0.0, duration=8.0, in_point=0.0)
        assert isinstance(clip_id_1, str)
        assert len(clip_id_1) > 0

        # b) clip_add(media_id, track_type, start_time, duration)
        clip_id_2 = client.clip_add(media_id, "audio", start_time=0.0, duration=15.0)
        assert isinstance(clip_id_2, str)
        assert len(clip_id_2) > 0

        # 5. keyframe_set
        kf_res = client.keyframe_set(clip_id=clip_id_1, property="scale", time=0.0, value=1.0)
        assert kf_res.get("status") == "ok"

        # 6. captions_add
        cap_res = client.captions_add(clip_id=clip_id_2, language="en")
        assert "captions_id" in cap_res or cap_res.get("status") == "ok"

        # 7. project_save
        save_res = client.project_save()
        assert save_res.get("status") == "ok"

        # 8. export_start & export_wait
        out_mp4 = tmp_path / "final.mp4"
        export_id = client.export_start(output_path=str(out_mp4), preset="youtube_shorts")
        assert isinstance(export_id, str)
        assert len(export_id) > 0

        wait_res = client.export_wait(export_id=export_id, timeout=10.0, poll_interval=0.05)
        assert wait_res.get("status") in ("completed", "success", "done")

        client.disconnect()


def test_rpc_error_handling(tmp_path: Path):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        with pytest.raises(ConcatRPCError) as exc_info:
            client.rpc_call("trigger_error", {"code": -32601, "message": "Method not found"})

        assert exc_info.value.code == -32601
        assert "Method not found" in exc_info.value.message
        client.disconnect()


def test_export_wait_timeout(tmp_path: Path):
    with MockConcatServer() as server:
        # Server configured to simulate indefinite progress
        server.set_export_behavior("always_pending")
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        export_id = client.export_start(output_path=str(tmp_path / "out.mp4"))
        with pytest.raises(ConcatExportTimeoutError, match="timed out"):
            client.export_wait(export_id=export_id, timeout=0.15, poll_interval=0.05)

        client.disconnect()


def test_export_wait_failed_status(tmp_path: Path):
    with MockConcatServer() as server:
        server.set_export_behavior("fail")
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        export_id = client.export_start(output_path=str(tmp_path / "out.mp4"))
        with pytest.raises(ConcatRPCError, match="Export failed"):
            client.export_wait(export_id=export_id, timeout=2.0, poll_interval=0.05)

        client.disconnect()


def test_offline_mock_mode_without_server(tmp_path: Path):
    # ConcatClient(mock=True) should operate completely offline without server or automation.json
    client = ConcatClient(mock=True)
    client.connect()

    ping_res = client.ping()
    assert ping_res.get("status") == "ok"

    proj_id = client.project_create(name="mock_short")
    assert proj_id.startswith("mock_proj_")

    client.project_set_video(1080, 1920, fps=30)
    media_id = client.media_import(str(tmp_path / "voice.wav"))
    assert media_id.startswith("mock_med_")

    clip_id = client.clip_add(media_id, "video", 0.0, 5.0)
    assert clip_id.startswith("mock_clip_")

    client.keyframe_set(clip_id, "scale", 0.0, 1.0)
    client.captions_add(clip_id)
    client.project_save()

    exp_id = client.export_start(str(tmp_path / "final.mp4"))
    wait_res = client.export_wait(exp_id, timeout=1.0)
    assert wait_res.get("status") == "completed"

    client.disconnect()


def test_reconnect_reloads_changed_automation_config(tmp_path: Path):
    config_file = tmp_path / "automation.json"
    with MockConcatServer() as server1:
        server1.write_automation_json(config_file)
        client = ConcatClient(config_path=config_file)
        client.connect()
        assert client.port == server1.port
        assert client.token == server1.token
        assert client.ping()["status"] == "ok"

        # Concat restarts on a new dynamic port with a new token
        with MockConcatServer() as server2:
            server2.write_automation_json(config_file)
            # Reconnect without recreating client instance
            client.connect()
            assert client.port == server2.port
            assert client.token == server2.token
            assert client.ping()["status"] == "ok"
            client.disconnect()


def test_clip_add_keyword_only_arguments(tmp_path: Path):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        client = ConcatClient(config_path=config_path)
        client.connect()

        # Invocation using track and media_id as keyword-only arguments
        clip_id_1 = client.clip_add(
            track="video", media_id="med_vid_123", start_time=0.0, duration=8.0
        )
        assert isinstance(clip_id_1, str)
        assert len(clip_id_1) > 0
        assert server.last_request is not None
        assert server.last_request["body"]["params"]["track"] == "video"
        assert server.last_request["body"]["params"]["media_id"] == "med_vid_123"

        # Invocation using track_type and media_id as keyword-only arguments
        clip_id_2 = client.clip_add(
            track_type="audio", media_id="med_aud_456", start_time=0.0, duration=15.0
        )
        assert isinstance(clip_id_2, str)
        assert len(clip_id_2) > 0
        assert server.last_request is not None
        assert server.last_request["body"]["params"]["track"] == "audio"
        assert server.last_request["body"]["params"]["media_id"] == "med_aud_456"

        client.disconnect()


def test_cli_ping_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    with MockConcatServer() as server:
        config_path = server.write_automation_json(tmp_path / "automation.json")
        ret_code = main(["--ping", "--config", str(config_path)])
        assert ret_code == 0
        captured = capsys.readouterr()
        assert "Connected to Concat Desktop" in captured.out
        assert str(server.port) in captured.out
        assert "0.2.9" in captured.out


def test_cli_ping_failure(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    non_existent = tmp_path / "non_existent.json"
    ret_code = main(["--ping", "--config", str(non_existent)])
    assert ret_code != 0
    captured = capsys.readouterr()
    assert "Failed to connect to Concat Desktop" in captured.err or "Error" in captured.err

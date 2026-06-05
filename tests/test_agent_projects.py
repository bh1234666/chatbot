import pytest
import asyncio


@pytest.mark.asyncio
async def test_agent_project_empty_dir_creates_chat_project(monkeypatch):
    from app.api import agent
    from app.core import environment_projects
    from app.schemas.api import AgentProjectCreateRequest

    mapping = {"projects": {}}
    created = []
    joined = []
    persona = []

    async def fake_create_archive(name):
        archive_id = f"arch_{len(created) + 1}"
        created.append(name)
        return {"archive_id": archive_id, "name": name, "created_at": "now"}

    async def fake_get_archive(archive_id):
        return None

    async def fake_get_persona_full(archive_id):
        return None

    async def fake_upsert_persona(archive_id, content):
        persona.append((archive_id, content))
        return {"archive_id": archive_id, "content": content, "updated_at": "now"}

    async def fake_join_group(group_id, archive_id, group_name="", persona_label=""):
        joined.append((group_id, archive_id, group_name, persona_label))
        return {"group_id": group_id, "active_archive_id": archive_id}

    class Persona:
        content = "bot persona"

    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: mapping)
    monkeypatch.setattr(environment_projects, "_write_mapping", lambda data: mapping.update(data))
    monkeypatch.setattr(environment_projects.archive_dao, "create_archive", fake_create_archive)
    monkeypatch.setattr(environment_projects.archive_dao, "get_archive", fake_get_archive)
    monkeypatch.setattr(agent.archive_dao, "get_persona_full", fake_get_persona_full)
    monkeypatch.setattr(agent.archive_dao, "upsert_persona", fake_upsert_persona)
    monkeypatch.setattr(agent.persona_files, "load_persona", lambda name: Persona())
    monkeypatch.setattr(agent.bot_config, "join_group", fake_join_group)

    result = await agent.create_project(AgentProjectCreateRequest(
        user_id="u",
        title="chat only",
        current_dir="",
    ))

    assert result.archive_id == "arch_1"
    assert result.group_id == "env_user_u"
    assert result.root_dir == ""
    assert result.project_name == "chat only"
    assert created == ["bot:u:chat only"]
    assert joined == [("env_user_u", "arch_1", "chat only", "bot")]
    assert persona == [("arch_1", "bot persona")]


@pytest.mark.asyncio
async def test_agent_project_non_empty_dir_rejects_missing_path(tmp_path):
    from app.api import agent
    from app.schemas.api import AgentProjectCreateRequest
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        await agent.create_project(AgentProjectCreateRequest(
            user_id="u",
            current_dir=str(tmp_path / "missing"),
        ))

    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_environment_project_mapping_survives_concurrent_writes(monkeypatch, tmp_path):
    from app.core import environment_projects

    mapping_path = tmp_path / "environment_projects.json"
    lock_path = tmp_path / ".environment_projects.lock"
    monkeypatch.setattr(environment_projects, "_MAPPING_PATH", mapping_path)
    monkeypatch.setattr(environment_projects, "_LOCK_PATH", lock_path)

    async def fake_create_archive(name):
        return {"archive_id": f"arch_{name.split(':')[-1]}", "name": name, "created_at": "now"}

    async def fake_get_archive(_archive_id):
        return None

    monkeypatch.setattr(environment_projects.archive_dao, "create_archive", fake_create_archive)
    monkeypatch.setattr(environment_projects.archive_dao, "get_archive", fake_get_archive)

    roots = []
    for idx in range(8):
        root = tmp_path / f"project_{idx}"
        root.mkdir()
        roots.append(root)

    results = await asyncio.gather(*[
        environment_projects.resolve_environment_project(
            user_id=f"user_{idx}",
            current_dir=str(root),
        )
        for idx, root in enumerate(roots)
    ])

    assert len(results) == 8
    data = environment_projects._read_mapping()
    assert len(data["projects"]) == 8
    assert {item["root_dir"] for item in data["projects"].values()} == {str(root.resolve()) for root in roots}


def test_agent_routes_registered():
    from app.main import app

    routes = {
        (getattr(route, "path", ""), tuple(sorted(getattr(route, "methods", []) or [])))
        for route in app.routes
    }

    assert ("/v1/agent/projects", ("GET",)) in routes
    assert ("/v1/agent/projects", ("POST",)) in routes
    assert ("/v1/agent/projects/{project_id}/tree", ("GET",)) in routes
    assert ("/v1/agent/projects/{project_id}/file", ("GET",)) in routes
    assert ("/v1/agent/projects/{project_id}/search", ("GET",)) in routes
    assert ("/v1/agent/projects/{project_id}/run", ("POST",)) in routes


def test_agent_project_list_filters_by_user(monkeypatch):
    from app.api import agent
    from app.core import environment_projects

    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: {
        "projects": {
            "u:p1": {
                "user_id": "u",
                "project_key": "p1",
                "archive_id": "arch_1",
                "group_id": "env_user_u",
                "root_dir": "",
                "project_name": "chat",
                "created_at": "1",
                "last_seen_at": "2",
            },
            "v:p2": {
                "user_id": "v",
                "project_key": "p2",
                "archive_id": "arch_2",
                "group_id": "env_user_v",
                "root_dir": "",
                "project_name": "other",
                "created_at": "1",
                "last_seen_at": "2",
            },
        }
    })

    result = agent.list_environment_projects("u")
    assert [item["archive_id"] for item in result] == ["arch_1"]


@pytest.mark.asyncio
async def test_agent_project_tree_and_file_preview(monkeypatch, tmp_path):
    from app.api import agent
    from app.core import environment_projects

    root = tmp_path / "proj"
    root.mkdir()
    (root / "README.md").write_text("# 标题\n正文", encoding="utf-8")
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text("print('ok')\n", encoding="utf-8")
    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: {
        "projects": {
            "u:p1": {
                "user_id": "u",
                "project_key": "p1",
                "archive_id": "arch_1",
                "group_id": "env_user_u",
                "root_dir": str(root),
                "project_name": "proj",
            },
        }
    })

    tree = await agent.project_tree("p1", user_id="u")
    assert any(item["path"] == "README.md" for item in tree["items"])

    preview = await agent.project_file("p1", user_id="u", path="README.md")
    assert preview["ok"] is True
    assert "标题" in preview["content"]

    search = await agent.project_search("p1", user_id="u", query="print")
    assert search["matches"][0]["path"] == "pkg/mod.py"


@pytest.mark.asyncio
async def test_agent_project_file_preview_preserves_utf8_chinese(monkeypatch, tmp_path):
    from app.api import agent
    from app.core import environment_projects

    root = tmp_path / "proj"
    root.mkdir()
    content = "# 半小时环境测试\n\n这是一个用于验证 agent/environment 模式的计算小项目。\n"
    (root / "README.md").write_text(content, encoding="utf-8")
    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: {
        "projects": {
            "u:p1": {
                "user_id": "u",
                "project_key": "p1",
                "archive_id": "arch_1",
                "group_id": "env_user_u",
                "root_dir": str(root),
                "project_name": "proj",
            },
        }
    })

    preview = await agent.project_file("p1", user_id="u", path="README.md")

    assert preview["ok"] is True
    assert preview["content"] == content


@pytest.mark.asyncio
async def test_agent_project_rejects_path_escape(monkeypatch, tmp_path):
    from app.api import agent
    from app.core import environment_projects
    from fastapi import HTTPException

    root = tmp_path / "proj"
    root.mkdir()
    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: {
        "projects": {
            "u:p1": {
                "user_id": "u",
                "project_key": "p1",
                "archive_id": "arch_1",
                "group_id": "env_user_u",
                "root_dir": str(root),
                "project_name": "proj",
            },
        }
    })

    with pytest.raises(HTTPException) as exc:
        await agent.project_file("p1", user_id="u", path="../secret.txt")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_agent_project_run_isolates_pytest_from_parent_repo(monkeypatch, tmp_path):
    from app.api import agent
    from app.core import environment_projects

    root = tmp_path / "proj"
    root.mkdir()
    captured = {}

    monkeypatch.setattr(environment_projects, "_read_mapping", lambda: {
        "projects": {
            "u:p1": {
                "user_id": "u",
                "project_key": "p1",
                "archive_id": "arch_1",
                "group_id": "env_user_u",
                "root_dir": str(root),
                "project_name": "proj",
            },
        }
    })

    class FakeProc:
        returncode = 0

        async def communicate(self):
            return b"ok\n", b""

    async def fake_create_subprocess_shell(command, **kwargs):
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["env"] = kwargs.get("env") or {}
        return FakeProc()

    monkeypatch.setattr(agent.asyncio, "create_subprocess_shell", fake_create_subprocess_shell)

    result = await agent.project_run("p1", {"command": "python -m pytest tests -q"}, user_id="u")

    assert result["ok"] is True
    assert captured["cwd"] == str(root.resolve())
    assert "python -m pytest --rootdir=." in captured["command"]
    assert ".env_pytest_empty.ini" in captured["command"]
    assert (root / ".env_pytest_empty.ini").is_file()
    assert captured["env"]["PYTHONIOENCODING"] == "utf-8"

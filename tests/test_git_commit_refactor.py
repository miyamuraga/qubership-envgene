import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path("python/envgene").resolve()))
sys.path.insert(0, str(Path("python/jschon-sort").resolve()))
sys.path.insert(0, str(Path("build_envgene/scripts").resolve()))
sys.path.insert(0, str(Path("build_pipegene/scripts").resolve()))

from envgenehelper.git_helper import GitRepoManager

GIT_COMMIT_PY = Path("build_envgene/scripts/git_commit.py")
PIPELINE_HELPER = Path("python/envgene/envgenehelper/pipeline_helper.py")

ENV_FILE = Path("environments/cluster/env/tenant.yml")


def load_module_from_path(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def write_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_git_commit_refactor_calls_git_manager_and_minimize(monkeypatch):
    """
    Проверяет, что git_commit.git_commit():
      - вызывает функцию минимизации diff'ов,
      - использует GitRepoManager и вызывает configure(), stage_changes(),
        create_detached_commit() и sync_and_push(sha).
    Все внешние операции замоканы.
    """
    git_commit_mod = load_module_from_path(GIT_COMMIT_PY, "gc_mod")

    calls = {"minimized": False, "configure_called": False, "staged": False, "created": None, "pushed": None}

    def fake_minimize():
        calls["minimized"] = True

    class FakeGitRepoManager:
        def __init__(self, *a, **kw):
            pass

        def configure(self):
            calls["configure_called"] = True

        def stage_changes(self):
            calls["staged"] = True
            return True  # симулируем, что есть изменения

        def create_detached_commit(self, message):
            calls["created"] = message
            return "deadbeefsha"

        def sync_and_push(self, sha):
            calls["pushed"] = sha

    monkeypatch.setattr(git_commit_mod, "minimize_cred_diffs", fake_minimize)
    monkeypatch.setattr(git_commit_mod, "GitRepoManager", FakeGitRepoManager)

    git_commit_mod.git_commit()

    assert calls["minimized"], "minimize_cred_diffs не был вызван"
    assert calls["configure_called"], "GitRepoManager.configure не был вызван"
    assert calls["staged"], "stage_changes() не был вызван/вернул False"
    assert calls["created"] is not None, "create_detached_commit не был вызван"
    assert calls["pushed"] == "deadbeefsha", "sync_and_push не был вызван с SHA"


def test_set_sparse_checkout_prepend_script():
    """
    Проверяет, что JobExtended.set_sparse_checkout добавляет в начало job.script вызов
    sparse-checkout (новый python-скрипт или, как запас, old .sh).
    """
    ph = load_module_from_path(PIPELINE_HELPER, "ph_mod")

    # создаём минимальный JobExtended (используем конструктор напрямую)
    job = ph.JobExtended(
        name="t",
        stage="s",
        image=None,
        script=["echo hello"],
        variables=None,
        timeout="10m",
    )

    job.set_sparse_checkout(["environments/cluster/env"])
    rendered = job.render()
    scripts = rendered.get("script", []) if isinstance(rendered, dict) else []

    assert any("sparse_checkout.py" in s or "sparse_checkout.sh" in s for s in scripts), (
        "Не найден prepend-скрипт sparse checkout в списке script"
    )


def test_sparse_checkout_uses_explicit_paths(monkeypatch):
    calls = []

    class FakeGit:
        def sparse_checkout(self, *args):
            calls.append(("sparse_checkout", args))

        def read_tree(self, *args):
            calls.append(("read_tree", args))

    manager = GitRepoManager.__new__(GitRepoManager)
    manager.repo = SimpleNamespace(git=FakeGit())
    manager.ctx = SimpleNamespace(commit_sha="abc123")

    def fake_fetch(**kwargs):
        calls.append(("fetch", kwargs))

    monkeypatch.setattr(manager, "_fetch", fake_fetch)
    monkeypatch.setattr(
        manager,
        "get_sparse_checkout_paths",
        lambda: pytest.fail("explicit sparse checkout paths must not be recomputed"),
    )

    manager.sparse_checkout(["custom/path", "environments/cluster/env"])

    assert calls == [
        (
            "fetch",
            {
                "ref": "abc123",
                "checkout": "abc123",
                "checkout_option": "--force",
                "create_remote": True,
            },
        ),
        ("sparse_checkout", ("init", "--cone")),
        ("sparse_checkout", ("set", "custom/path", "environments/cluster/env")),
        ("read_tree", ("-mu", "HEAD")),
    ]


def test_get_sparse_checkout_paths_accepts_explicit_environment_name(monkeypatch):
    monkeypatch.delenv("FULL_ENV_NAME", raising=False)

    paths = GitRepoManager.get_sparse_checkout_paths("cluster-02/env-02", include_full_cluster=True)

    assert "configuration/" in paths
    assert "environments/cluster-02/env-02" in paths
    assert "environments/cluster-02/cloud-passport" in paths
    assert "environments/cluster-02/" in paths


def test_git_helper_does_not_import_pipeline_helper():
    git_helper_source = Path("python/envgene/envgenehelper/git_helper.py").read_text(encoding="utf-8")

    assert "envgenehelper.pipeline_helper" not in git_helper_source


def test_sync_and_push_cherry_picks_snapshot_without_silently_overwriting_remote_head(tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    pipeline_repo = tmp_path / "pipeline"
    actor = tmp_path / "actor"
    verifier = tmp_path / "verifier"

    run_git(["init", "--bare", str(remote)], tmp_path)

    seed.mkdir()
    run_git(["init", "-b", "main"], seed)
    run_git(["config", "user.email", "seed@example.com"], seed)
    run_git(["config", "user.name", "seed"], seed)
    write_file(seed / ENV_FILE, "value: B\n")
    run_git(["add", str(ENV_FILE)], seed)
    run_git(["commit", "-m", "B"], seed)
    run_git(["remote", "add", "origin", str(remote)], seed)
    run_git(["push", "-u", "origin", "main"], seed)
    run_git(["symbolic-ref", "HEAD", "refs/heads/main"], remote)
    initial_sha = run_git(["rev-parse", "HEAD"], seed)

    pipeline_repo.mkdir()
    monkeypatch.setenv("CI_PROJECT_DIR", str(pipeline_repo))
    monkeypatch.setenv("GITLAB_CI", "true")
    monkeypatch.setenv("CI_SERVER_PROTOCOL", "file")
    monkeypatch.setenv("CI_SERVER_HOST", "local")
    monkeypatch.setenv("CI_PROJECT_PATH", "group/project")
    monkeypatch.setenv("CI_COMMIT_REF_NAME", "main")
    monkeypatch.setenv("GITLAB_USER_EMAIL", "ci@example.com")
    monkeypatch.setenv("GITLAB_USER_LOGIN", "ci")
    monkeypatch.setenv("GITLAB_TOKEN", "token")
    monkeypatch.setenv("CI_COMMIT_SHA", initial_sha)

    monkeypatch.setattr(GitRepoManager, "resolve_remote_url", lambda self: str(remote))

    manager = GitRepoManager()
    manager.configure()
    manager.sparse_checkout(["environments/cluster/env"])

    write_file(pipeline_repo / ENV_FILE, "value: pipeline-change\n")
    assert manager.stage_changes()
    snapshot_sha = manager.create_detached_commit("pipeline change")

    run_git(["clone", str(remote), str(actor)], tmp_path)
    run_git(["config", "user.email", "actor@example.com"], actor)
    run_git(["config", "user.name", "actor"], actor)
    write_file(actor / ENV_FILE, "value: remote-D\n")
    run_git(["commit", "-am", "D"], actor)
    run_git(["push", "origin", "main"], actor)

    with pytest.raises(RuntimeError, match="Push failed"):
        manager._sync_and_push(snapshot_sha)

    run_git(["clone", str(remote), str(verifier)], tmp_path)
    assert (verifier / ENV_FILE).read_text(encoding="utf-8") == "value: remote-D\n"

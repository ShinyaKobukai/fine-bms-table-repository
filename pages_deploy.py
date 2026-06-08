import os
import shutil
import subprocess
from pathlib import Path


class DeployError(Exception):
    pass


def env_value(name, default=""):
    return os.getenv(name, default).strip()


def run_git(repo_dir, args, env=None):
    cmd = ["git", "-C", str(repo_dir)] + args
    try:
        return subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or str(exc)).strip()
        raise DeployError(message or "git command failed") from exc


def copy_user_tables(source_tables_dir, pages_repo_dir, tables_subdir, user_id):
    source_user_dir = Path(source_tables_dir) / "users" / str(user_id)
    if not source_user_dir.exists():
        raise DeployError(f"generated tables not found: {source_user_dir}")

    target_user_dir = Path(pages_repo_dir) / tables_subdir / "users" / str(user_id)
    target_user_dir.parent.mkdir(parents=True, exist_ok=True)

    if target_user_dir.exists():
        shutil.rmtree(target_user_dir)
    shutil.copytree(source_user_dir, target_user_dir)
    return target_user_dir


def deploy_user_tables(user_id, source_tables_dir="public/tables"):
    pages_repo_dir = env_value("GITHUB_PAGES_DIR")
    if not pages_repo_dir:
        raise DeployError("GITHUB_PAGES_DIR is not set")

    pages_repo_dir = Path(pages_repo_dir).expanduser().resolve()
    if not pages_repo_dir.exists():
        raise DeployError(f"GITHUB_PAGES_DIR does not exist: {pages_repo_dir}")

    tables_subdir = env_value("GITHUB_PAGES_TABLES_DIR", "tables").strip("/\\")
    remote = env_value("GITHUB_PAGES_REMOTE", "origin")
    branch = env_value("GITHUB_PAGES_BRANCH", "")

    target_user_dir = copy_user_tables(source_tables_dir, pages_repo_dir, tables_subdir, user_id)
    target_rel = target_user_dir.relative_to(pages_repo_dir).as_posix()

    run_git(pages_repo_dir, ["add", target_rel])
    diff = subprocess.run(
        ["git", "-C", str(pages_repo_dir), "diff", "--cached", "--quiet", "--", target_rel],
        capture_output=True,
        text=True,
    )
    if diff.returncode == 0:
        return {
            "committed": False,
            "pushed": False,
            "message": "no changes",
            "target": str(target_user_dir),
        }
    if diff.returncode not in (0, 1):
        raise DeployError((diff.stderr or diff.stdout or "git diff failed").strip())

    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", env_value("GITHUB_PAGES_COMMIT_NAME", "Fine Bot"))
    env.setdefault("GIT_AUTHOR_EMAIL", env_value("GITHUB_PAGES_COMMIT_EMAIL", "fine-bot@example.local"))
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

    run_git(
        pages_repo_dir,
        ["commit", "-m", f"Update Fine BMS tables for user {user_id}"],
        env=env,
    )

    push_args = []
    token = env_value("GITHUB_TOKEN") or env_value("GH_TOKEN")
    if token:
        push_args += [
            "-c",
            f"http.https://github.com/.extraheader=AUTHORIZATION: bearer {token}",
        ]
    push_args += ["push", remote]
    if branch:
        push_args.append(branch)

    run_git(pages_repo_dir, push_args, env=env)
    return {
        "committed": True,
        "pushed": True,
        "message": "pushed",
        "target": str(target_user_dir),
    }

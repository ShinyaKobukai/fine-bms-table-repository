import os
import logging
import shutil
import subprocess
import time
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


def current_commit(repo_dir):
    result = run_git(repo_dir, ["rev-parse", "HEAD"])
    return result.stdout.strip()


def git_auth_args():
    token = env_value("GITHUB_TOKEN") or env_value("GH_TOKEN")
    if not token:
        return []
    return [
        "-c",
        f"http.https://github.com/.extraheader=AUTHORIZATION: bearer {token}",
    ]


def is_push_rejected(error):
    text = str(error).lower()
    rejected_markers = [
        "rejected",
        "fetch first",
        "non-fast-forward",
        "remote contains work",
    ]
    return any(marker in text for marker in rejected_markers)


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


def deploy_user_tables(user_id, source_tables_dir="public/tables", progress=None):
    total_started = time.perf_counter()
    timings = {}
    pages_repo_dir = env_value("GITHUB_PAGES_DIR")
    if not pages_repo_dir:
        raise DeployError("GITHUB_PAGES_DIR is not set")

    pages_repo_dir = Path(pages_repo_dir).expanduser().resolve()
    if not pages_repo_dir.exists():
        raise DeployError(f"GITHUB_PAGES_DIR does not exist: {pages_repo_dir}")

    tables_subdir = env_value("GITHUB_PAGES_TABLES_DIR", "tables").strip("/\\")
    remote = env_value("GITHUB_PAGES_REMOTE", "origin")
    branch = env_value("GITHUB_PAGES_BRANCH", "main")
    auth_args = git_auth_args()
    env = os.environ.copy()

    if progress:
        progress("pull")
    started = time.perf_counter()
    run_git(pages_repo_dir, auth_args + ["pull", "--rebase", remote, branch], env=env)
    timings["git_pull"] = time.perf_counter() - started

    if progress:
        progress("copy")
    started = time.perf_counter()
    target_user_dir = copy_user_tables(source_tables_dir, pages_repo_dir, tables_subdir, user_id)
    timings["copy"] = time.perf_counter() - started
    target_rel = target_user_dir.relative_to(pages_repo_dir).as_posix()

    if progress:
        progress("commit")
    started = time.perf_counter()
    run_git(pages_repo_dir, ["add", target_rel])
    timings["git_add"] = time.perf_counter() - started
    started = time.perf_counter()
    diff = subprocess.run(
        ["git", "-C", str(pages_repo_dir), "diff", "--cached", "--quiet", "--", target_rel],
        capture_output=True,
        text=True,
    )
    timings["git_diff"] = time.perf_counter() - started
    if diff.returncode == 0:
        commit_hash = current_commit(pages_repo_dir)
        if progress:
            progress("complete", status="skipped", commit=commit_hash)
        logging.info(
            "pages_deploy timings user_id=%s git_pull=%.3fs copy=%.3fs git_add=%.3fs git_diff=%.3fs total=%.3fs result=skipped commit=%s",
            user_id,
            timings["git_pull"],
            timings["copy"],
            timings["git_add"],
            timings["git_diff"],
            time.perf_counter() - total_started,
            commit_hash,
        )
        return {
            "committed": False,
            "pushed": False,
            "message": "skipped",
            "commit": commit_hash,
            "target": str(target_user_dir),
        }
    if diff.returncode not in (0, 1):
        raise DeployError((diff.stderr or diff.stdout or "git diff failed").strip())

    env.setdefault("GIT_AUTHOR_NAME", env_value("GITHUB_PAGES_COMMIT_NAME", "Fine Bot"))
    env.setdefault("GIT_AUTHOR_EMAIL", env_value("GITHUB_PAGES_COMMIT_EMAIL", "fine-bot@example.local"))
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])

    started = time.perf_counter()
    run_git(
        pages_repo_dir,
        ["commit", "-m", f"Update Fine BMS tables for user {user_id}"],
        env=env,
    )
    timings["git_commit"] = time.perf_counter() - started

    if progress:
        progress("push")
    push_args = auth_args + ["push", remote, branch]
    push_retried = False
    started = time.perf_counter()
    try:
        run_git(pages_repo_dir, push_args, env=env)
    except DeployError as exc:
        if not is_push_rejected(exc):
            raise
        push_retried = True
        run_git(pages_repo_dir, auth_args + ["pull", "--rebase", remote, branch], env=env)
        run_git(pages_repo_dir, push_args, env=env)
    timings["git_push"] = time.perf_counter() - started
    commit_hash = current_commit(pages_repo_dir)
    if progress:
        progress("complete", status="pushed", commit=commit_hash)
    logging.info(
        "pages_deploy timings user_id=%s git_pull=%.3fs copy=%.3fs git_add=%.3fs git_diff=%.3fs git_commit=%.3fs git_push=%.3fs total=%.3fs push_retried=%s result=pushed commit=%s",
        user_id,
        timings["git_pull"],
        timings["copy"],
        timings["git_add"],
        timings["git_diff"],
        timings["git_commit"],
        timings["git_push"],
        time.perf_counter() - total_started,
        push_retried,
        commit_hash,
    )
    return {
        "committed": True,
        "pushed": True,
        "message": "pushed",
        "commit": commit_hash,
        "target": str(target_user_dir),
    }

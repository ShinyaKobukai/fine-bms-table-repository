import argparse
import json
from pathlib import Path


def iter_data_files(root, user_id=None):
    root = Path(root)
    users_root = root / "users"
    if user_id:
        yield from (users_root / str(user_id) / "tags").glob("**/data.json")
    else:
        yield from users_root.glob("*/tags/**/data.json")


def summarize(root, user_id=None):
    hit = 0
    missing = []
    files = 0

    for path in iter_data_files(root, user_id=user_id):
        files += 1
        data = json.loads(path.read_text(encoding="utf-8"))
        tag = data.get("tag", "")
        for song in data.get("songs", []):
            if song.get("md5"):
                hit += 1
            else:
                missing.append(
                    {
                        "tag": tag,
                        "level": song.get("level", ""),
                        "title": song.get("display_title") or song.get("title", ""),
                        "path": str(path),
                    }
                )

    return {
        "files": files,
        "md5_hit": hit,
        "md5_missing": len(missing),
        "missing": missing,
    }


def main():
    parser = argparse.ArgumentParser(description="Summarize md5 hit/missing for generated Fine tables.")
    parser.add_argument("--root", default="public/tables", help="Generated table root directory.")
    parser.add_argument("--user-id", default=None, help="Optional Discord user_id to inspect.")
    parser.add_argument("--show-missing", action="store_true", help="Print missing song list.")
    args = parser.parse_args()

    result = summarize(args.root, user_id=args.user_id)
    print(f"files={result['files']}")
    print(f"md5_hit={result['md5_hit']}")
    print(f"md5_missing={result['md5_missing']}")

    if args.show_missing:
        for item in result["missing"]:
            print(f"{item['tag']}\t{item['level']}\t{item['title']}")


if __name__ == "__main__":
    main()

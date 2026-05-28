#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import tarfile
import time
from datetime import datetime
from pathlib import Path


RUN_NAME = "v7_live_20260525"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def iter_files(root: Path, paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            files.append(path)
            continue
        for child in path.rglob("*"):
            if child.is_file():
                files.append(child)
    unique = sorted({p.resolve() for p in files})
    return unique


def prune_archives(archive_dir: Path, keep_days: int) -> int:
    if keep_days <= 0 or not archive_dir.exists():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for path in archive_dir.glob("3pips_paper_*.tar.gz"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except FileNotFoundError:
            continue
    return removed


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", default=str(Path(__file__).resolve().parents[1]))
    parser.add_argument("--run-name", default=RUN_NAME)
    parser.add_argument("--archive-dir", default="")
    parser.add_argument("--keep-days", type=int, default=14)
    args = parser.parse_args()

    root = Path(args.project_root).resolve()
    run_dir = root / "reports" / "paper_runs" / args.run_name
    runtime_dir = root / "reports" / "runtime"
    archive_dir = Path(args.archive_dir).resolve() if args.archive_dir else root / "reports" / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_path = archive_dir / f"3pips_paper_{args.run_name}_{stamp}.tar.gz"

    include_paths = [
        run_dir,
        runtime_dir / "v7_paper_supervisor_20260525.log",
        runtime_dir / "v7_paper_supervisor_20260525.pid",
        runtime_dir / "v7_paper_dashboard_20260525.pid",
        root / "reports" / "futures_scalp_profiles_v7_paper_20260525.csv",
        root / "reports" / "futures_scalp_profiles_v7_paper_20260525_gpt_shadow_params.csv",
        root / "reports" / "stock_moex_scalp_results_review" / "stock_final_live_paper_profiles.json",
    ]
    files = iter_files(root, include_paths)
    manifest = {
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "project_root": str(root),
        "run_name": args.run_name,
        "file_count": len(files),
        "files": [],
    }

    with tarfile.open(archive_path, "w:gz") as tar:
        for path in files:
            rel = path.relative_to(root)
            stat = path.stat()
            manifest["files"].append(
                {
                    "path": str(rel).replace("\\", "/"),
                    "size": stat.st_size,
                    "mtime": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                    "sha256": sha256_file(path),
                }
            )
            tar.add(path, arcname=str(rel).replace("\\", "/"))

        manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
        info = tarfile.TarInfo("ARCHIVE_MANIFEST.json")
        info.size = len(manifest_bytes)
        info.mtime = time.time()
        import io

        tar.addfile(info, io.BytesIO(manifest_bytes))

    removed = prune_archives(archive_dir, args.keep_days)
    print(
        json.dumps(
            {
                "archive": str(archive_path),
                "files": len(files),
                "size": archive_path.stat().st_size,
                "removed_old_archives": removed,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()

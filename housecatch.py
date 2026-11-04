"""하우스캐치 블로그: /housecatch가 부르는 명령 하나.

  python/python.exe -X utf8 housecatch.py [편수] [--topic '주제' ...]

먼저 install/update.py로 이 레포(github.com/tom-heo-biik/housecatch)의 새 판을 받고, 그다음 두루미 batch로
블로그를 한꺼번에 씁니다. 주제를 주지 않으면 두루미의 주제 노드가 지난 주제와 겹치지 않는 주제를 정해 편수만큼
(기본 5편), 주제를 주면 주제마다 한 편을 씁니다. 끝난 글은 블로그/발행 전 폴더로 옮겨집니다. 업데이트가 이 파일을
바꿔도 이번 실행은 이미 읽은 판으로 끝까지 갑니다.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRAND = "housecatch"
COUNT, MOST = 5, 10


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="housecatch", description="하우스캐치 블로그를 한꺼번에 씁니다")
    parser.add_argument("count", nargs="?", type=int, default=COUNT, help="주제를 주지 않을 때 쓸 편수(기본 5)")
    parser.add_argument("--topic", action="append", default=[], help="쓸 주제. 여러 번 주면 주제마다 한 편")
    args = parser.parse_args(argv)
    count = max(1, min(MOST, args.count))
    given = [topic for topic in args.topic if topic.strip()][:MOST]
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    python = sys.executable
    subprocess.run([python, "-X", "utf8", str(ROOT / "install" / "update.py")], env=env, check=False)
    sys.stdout.flush()
    outbox = ROOT / "블로그" / "발행 전"
    command = [python, "-X", "utf8", str(ROOT / "durumi" / "tools" / "durumi.py"), "batch", BRAND,
               "--root", str(ROOT / "durumi"), "--count", str(count), "--outbox", str(outbox)]
    for topic in given:
        command += ["--topic", topic]
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))

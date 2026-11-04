"""하우스캐치 블로그: /housecatch가 부르는 명령 하나.

  python/python.exe -X utf8 housecatch.py [편수]

먼저 install/update.py로 이 레포(github.com/tom-heo-biik/housecatch)의 새 판을 받고, 그다음 두루미 batch로
블로그를 편수만큼(기본 5편) 한꺼번에 씁니다. 주제는 지난 주제와 겹치지 않게 두루미의 주제 노드가 정하고,
끝난 글은 블로그/발행 전 폴더로 옮겨집니다. 업데이트가 이 파일을 바꿔도 이번 실행은 이미 읽은 판으로 끝까지 갑니다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BRAND = "housecatch"
COUNT, MOST = 5, 10


def main(argv: list[str]) -> int:
    count = COUNT
    if argv and argv[0].strip().isdigit():
        count = max(1, min(MOST, int(argv[0].strip())))
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    python = sys.executable
    subprocess.run([python, "-X", "utf8", str(ROOT / "install" / "update.py")], env=env, check=False)
    sys.stdout.flush()
    outbox = ROOT / "블로그" / "발행 전"
    command = [python, "-X", "utf8", str(ROOT / "durumi" / "tools" / "durumi.py"), "batch", BRAND,
               "--root", str(ROOT / "durumi"), "--count", str(count), "--outbox", str(outbox)]
    return subprocess.run(command, env=env, check=False).returncode


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env bash
# .claude/hooks/le-mot-juste-markdown.sh
# PreToolUse 훅(Write, Edit, MultiEdit). 마크다운 문서를 쓰거나 고치기 전에 Le-mot-juste 형식을 검사합니다.
#   판정은 같은 폴더의 le_mot_juste_markdown.py가 하고, 이 스크립트는 파이썬을 찾아 입력을 넘깁니다.
#   새로 생기는 위반이 있으면 exit 2로 도구 호출을 막고, stderr의 사유가 Claude에게 갑니다.
#   마크다운(.md, .markdown)이 아니면 파이썬을 띄우지 않고 바로 통과합니다.
#   파이썬 3.8 이상을 찾지 못하면 막지 않고 exit 1로 그 사실만 알립니다.
# macOS 기본 bash 3.2에서도 돌도록 bash 4 문법은 쓰지 않습니다.

input="$(cat)"
shopt -s nocasematch
case "$input" in
  *'.md"'* | *'.markdown"'*) ;;
  *) exit 0 ;;
esac
shopt -u nocasematch

# Git Bash의 pwd -W는 C:/ 꼴 경로를 냅니다. MSYS_NO_PATHCONV=1처럼 경로 변환을 끈 세션에서도 파이썬이
# 검사기를 찾게 하려는 것이고, -W가 없는 macOS와 리눅스의 bash는 오류를 내고 pwd로 넘어갑니다.
here="$(cd "$(dirname "$0")" && { pwd -W 2>/dev/null || pwd; })"
# 버전 확인과 실행을 파이썬 한 번으로 합칩니다. 3.8보다 낮으면 3으로 끝나 다음 후보로 넘어갑니다.
# 검사기는 0(통과), 2(막음), 1(검사기 오류)만 돌려주니 그 밖의 코드는 쓸 수 없는 파이썬입니다.
boot='import sys
sys.version_info >= (3, 8) or sys.exit(3)
path = sys.argv[1]
sys.argv = [path, "hook"]
exec(compile(open(path, encoding="utf-8").read(), path, "exec"), {"__name__": "__main__", "__file__": path})'
# 레포 맨 위 python/에 파이썬을 함께 넣어 두는 레포(하우스캐치 블로그 레포)에서는 그 파이썬을 먼저 씁니다.
for py in "$here/../../python/python.exe" python python3 py; do
  command -v "$py" >/dev/null 2>&1 || continue
  args=()
  [ "$py" = py ] && args=(-3)
  printf '%s' "$input" | PYTHONUTF8=1 "$py" ${args[@]+"${args[@]}"} -c "$boot" "$here/le_mot_juste_markdown.py"
  code=$?
  case "$code" in 0 | 1 | 2) exit "$code" ;; esac
done
echo "Le-mot-juste 형식 검사를 건너뛰었습니다. 파이썬 3.8 이상을 찾지 못했습니다." >&2
exit 1

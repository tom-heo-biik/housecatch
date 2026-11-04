"""Generate the SmartEditor ONE paste-preservation test sheets.

A person opens each sheet in a browser, selects all, copies, pastes into a new SmartEditor ONE post,
and records per case whether it survived in docs/smarteditor/results.tsv. Part A is what the renderer
emits; part B re-checks forms the crane rules say are not preserved. Sheets are regenerated from rules,
so they stay in step with the renderer.
"""
from __future__ import annotations

import html
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

import config  # noqa: E402
import markup  # noqa: E402
import render  # noqa: E402


def _body_inner(full_html: str) -> str:
    match = re.search(r"<body>\n(.*)\n</body>", full_html, re.S)
    return match.group(1) if match else full_html


def _rendered(rules, brand, body: str, palettes=("blue", "pink"), title="시험") -> str:
    envelope = {"slug": "preservation", "title": title, "palettes": list(palettes), "body": body}
    doc, issues = markup.parse(envelope, rules, brand)
    if doc is None:
        raise SystemExit("시험지 원고가 파싱되지 않았습니다: " + "; ".join(i.message for i in issues))
    inner = _body_inner(render.render(doc, rules))
    # drop the title and the gap after it; the sheet has its own case labels
    return re.sub(r"\A<h1>.*?</h1>\n<br>\n", "", inner, flags=re.S)


def _label(case_id: str, text: str) -> str:
    return f'<p><strong>[{case_id}] {html.escape(text)}</strong></p>'


def cases_a(rules, brand) -> list[tuple[str, str, str, str]]:
    pal = rules.palettes
    colors = "<br>".join(
        f'<strong><span style="color:{p.text};">{html.escape(p.label)} 글자 색 {p.text}</span></strong>' for p in pal.values()
    )
    highlights = "<br>".join(
        f'<span style="background-color:{p.highlight};">{html.escape(p.label)} 형광펜 {p.highlight}</span>' for p in pal.values()
    )
    emoji = "<br>".join(f"{e} {html.escape(m)}" for e, m in rules.form["emoji"]["meaning"].items())
    return [
        ("A01", "제목 h1", "제목 칸으로 가는지, 본문에 큰 글씨로 남는지 기록", "<h1>보존 시험 제목</h1>"),
        ("A02", "소제목 24px 굵게", "24px 굵은 검정 글씨", _rendered(rules, brand, "도입.\n\n## 소제목 보존 시험").split("<br>\n<br>\n<br>\n", 1)[-1]),
        ("A03", "문단과 문단 안 줄바꿈", "한 문단 안에서 줄이 바뀜", _rendered(rules, brand, "첫 줄입니다.\n둘째 줄입니다.")),
        ("A04", "굵게", "굵은 검정 글씨", _rendered(rules, brand, "여기 **굵게** 보존 시험입니다.")),
        ("A05", "글자 색 여섯", "여섯 색이 굵게와 함께 유지", f"<p>{colors}</p>"),
        ("A06", "형광펜 여섯", "여섯 배경색이 유지", f"<p>{highlights}</p>"),
        ("A07", "인용구", "따옴표형 인용구로 들어옴(다른 종류면 기록)", _rendered(rules, brand, "도입.\n\n> 인용 첫 줄\n> 인용 둘째 줄").split("<br>\n", 1)[-1]),
        ("A08", "순서 없는 목록", "점 목록 세 항목", _rendered(rules, brand, "도입.\n\n- 가\n- 나\n- 다").split("<br>\n", 1)[-1]),
        ("A09", "순서 있는 목록", "1부터 번호 목록", _rendered(rules, brand, "도입.\n\n1. 하나\n2. 둘\n3. 셋").split("<br>\n", 1)[-1]),
        ("A10", "체크리스트", "☑️와 줄바꿈 유지", _rendered(rules, brand, "도입.\n\n☑️ 확인 하나\n☑️ 확인 둘").split("<br>\n", 1)[-1]),
        ("A11", "표와 가부 표시", "머리글 행과 테두리, ✅ 🔴 유지", _rendered(rules, brand, "도입.\n\n| 항목 | 가능 |\n|---|---|\n| 가 | ✅ |\n| 나 | 🔴 |").split("<br>\n", 1)[-1]),
        ("A12", "이미지 자리", "검정 대괄호와 초록 글자", _rendered(rules, brand, "도입.\n\n[이미지: 교실 창가 자리 사진]").split("<br>\n", 1)[-1]),
        ("A13", "링크 자리", "검정 대괄호와 초록 글자", _rendered(rules, brand, "도입.\n\n[링크: 공식 홈페이지]").split("<br>\n", 1)[-1]),
        ("A14", "이모지 여덟", "여덟 가지가 모두 그림 문자로 유지", f"<p>{emoji}</p>"),
        ("A15", "간격 br 하나와 셋", "위 간격은 한 줄, 아래 간격은 세 줄", "<p>간격 위</p>\n<br>\n<p>br 하나 뒤</p>\n<br>\n<br>\n<br>\n<p>br 셋 뒤</p>"),
    ]


def cases_b() -> list[tuple[str, str, str, str]]:
    return [
        ("B01", "mark 형광펜", "크레인 규칙상 보존 안 됨", "<p><mark>mark 형광펜</mark></p>"),
        ("B02", "font 글자 색", "크레인 규칙상 폐기 태그", '<p><font color="#0072CE">font 글자 색</font></p>'),
        ("B03", "h2 소제목", "크레인 규칙상 과거 형식", "<h2>h2 소제목</h2>"),
        ("B04", "중첩 목록", "크레인 규칙상 들여쓰기 보존 안 됨", "<ul><li>바깥<ul><li>안쪽</li></ul></li></ul>"),
        ("B05", "ol start", "크레인 규칙상 start 무시", '<ol start="3"><li>셋부터?</li><li>다음</li></ol>'),
        ("B06", "자간과 폰트", "크레인 규칙상 보존 안 됨", '<p><span style="letter-spacing:4px;font-family:serif;">자간과 폰트</span></p>'),
        ("B07", "pre 코드블록", "크레인 규칙상 보존 안 됨", "<pre>pre 코드블록</pre>"),
        ("B08", "세로줄 인용구", "크레인 규칙상 따옴표형만 가능", '<blockquote style="border-left:4px solid #dddddd;padding-left:8px;">세로줄 인용구</blockquote>'),
        ("B09", "목록 밖 이모지", "허용 여덟 밖 그림 문자", "<p>🙂 ⭐ ❤️ 👉 ✔️ ❗ 🟢</p>"),
        ("B10", "기울임, 밑줄, 취소선", "크레인 규칙상 쓰지 않음", "<p><em>em</em> <i>i</i> <u>u</u> <del>del</del> <s>s</s></p>"),
        ("B11", "빈 문단 간격", "크레인 규칙상 씹힘", "<p>빈 문단 위</p><p></p><p></p><p>빈 문단 아래</p>"),
        ("B12", "a href 링크", "크레인 규칙상 연결 사라짐", '<p><a href="https://example.com">a href 링크</a></p>'),
        ("B13", "외부 img", "크레인 규칙상 인식 안 됨", '<p><img src="https://example.com/a.png" alt="외부 이미지"></p>'),
        ("B14", "셀 배경과 병합", "크레인 규칙상 쓸 수 있다고 적힘", '<table><thead><tr><th colspan="2">병합 머리글</th></tr></thead><tbody><tr><td style="background-color:#9BCBEB;">배경</td><td>보통</td></tr></tbody></table>'),
        ("B15", "20px 글씨", "16px와 24px 밖 크기", '<p><span style="font-size:20px;">20px 글씨</span></p>'),
        ("B16", "hr 구분선", "규칙에 없는 태그", "<hr>"),
    ]


def sheet(rules, title: str, cases, skeleton: str = "style") -> str:
    style = rules.form["html"]["style"]
    head = ["<!doctype html>", '<html lang="ko">', "<head>", '<meta charset="utf-8">', f"<title>{html.escape(title)}</title>"]
    body_open = "<body>"
    if skeleton == "style":
        head.append(f"<style>{style}</style>")
    elif skeleton == "body-attr":
        body_open = '<body style="font-size:16px; color:#000000;">'
    elif skeleton == "div":
        pass
    head.append("</head>")
    parts = []
    for case_id, name, expected, content in cases:
        parts += [_label(case_id, f"{name} (기대: {expected})"), "<br>", content, "<br>", "<br>", "<br>"]
    inner = "\n".join(parts[:-3]) if parts else ""
    if skeleton == "div":
        inner = f'<div style="font-size:16px;">\n{inner}\n</div>'
    return "\n".join(head + [body_open, inner, "</body>", "</html>"]) + "\n"


def results_template(cases) -> str:
    rows = ["번호\t시험 항목\t기대\t결과(보존, 깨짐, 부분)\t비고\t시험일"]
    rows += [f"{c}\t{n}\t{e}\t\t\t" for c, n, e, _ in cases]
    return "\n".join(rows) + "\n"


def main(argv=None) -> int:
    root, _ = config.find_root()
    rules = config.load_rules(root)
    brand = config.load_brand(root, "durumi")
    out = root / "docs" / "smarteditor"
    out.mkdir(parents=True, exist_ok=True)
    a, b = cases_a(rules, brand), cases_b()
    (out / "sheet-main.html").write_text(sheet(rules, "스마트에디터 ONE 보존 시험 본", a + b), encoding="utf-8", newline="\n")
    base16 = [("S01", "16px 본문", "16px 검정 글씨로 들어옴", "<p>이 문단은 16px 기본값이어야 합니다.</p>")]
    for kind in ("style", "body-attr", "div"):
        (out / f"sheet-skeleton-{kind}.html").write_text(sheet(rules, f"16px 뼈대 시험 {kind}", base16, skeleton=kind), encoding="utf-8", newline="\n")
    results = out / "results.tsv"
    if not results.exists():
        results.write_text(results_template(a + b + [("S-style", "16px 뼈대 style", "16px", ""), ("S-body-attr", "16px 뼈대 body 속성", "16px", ""), ("S-div", "16px 뼈대 감싸개", "16px", "")]), encoding="utf-8", newline="\n")
    print(f"시험지를 만들었습니다: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

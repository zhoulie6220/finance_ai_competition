"""打一个**双击就能打开**的单文件网页，发给会计同学看。

用法：
    python scripts/build_offline_page.py --out "D:/.../离线工作台"

做三件事：

    1. 导全量接口快照   → `export_demo_snapshot.py`
    2. 构建前端         → `npm run build`（在 frontend/ 下）
    3. 把 JS / CSS / 数据**全部内联**进一个 `index.html`

## 为什么必须是「一个 HTML」

`file://` 下浏览器**禁止** `fetch` 本地文件（CORS），也**禁止**
`<script type="module" src="...">` 跨文件加载。所以：
把一个文件夹发过去、让他双击里面的 `index.html` 是**打不开的**——
白屏，而且不报错。所有东西内联成一个文件，这两个限制就都不存在了。

## 为什么不用「打包 Python 环境」

会计同学不装 Python。带上解释器和依赖是几百 MB，还得教他怎么启动、
怎么关、端口被占了怎么办。**而我们真正要给他看的是数据，不是服务。**
冻成快照之后，一个文件、双击、断网也能看。

## ⚠ 内联时的转义

JS 里可能出现 `</script>`（字符串常量、正则），直接塞进 `<script>` 会让
HTML 解析器提前结束那个标签——**整页白屏，而且不报错**。
所以把 `</script` 一律改写成 `<\\/script`：在 JS 字符串里两者等价，
但 HTML 解析器不再认它是结束标签。JSON 同理（且 `\\/` 是合法转义）。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _console  # noqa: E402

_console.setup()

BACKEND = Path(__file__).resolve().parent.parent
FRONTEND = BACKEND.parent / "frontend"


def _safe(text: str) -> str:
    """让一段文本可以安全地放进 `<script>` 里。见模块头的说明。"""
    return text.replace("</script", "<\\/script")


def build(out_dir: Path, *, banner: bool = True) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = out_dir / "_snapshot.json"

    print("[1/3] 导接口快照…")
    r = subprocess.run(
        [sys.executable, str(BACKEND / "scripts" / "export_demo_snapshot.py"),
         "--out", str(snapshot_path)],
        cwd=BACKEND,
    )
    if r.returncode != 0:
        print("✗ 快照导出失败，中止。**不要打一个半成品发出去**——"
              "页面上缺的那块看起来就像「本来没数据」。")
        return r.returncode

    print("[2/3] 构建前端（IIFE 产物，见 vite.config.offline.ts）…")
    r = subprocess.run(
        ["npx", "vite", "build", "--config", "vite.config.offline.ts"],
        cwd=FRONTEND, shell=True,
    )
    if r.returncode != 0:
        print("✗ 前端构建失败，中止。")
        return r.returncode

    print("[3/3] 内联成单文件…")
    dist = FRONTEND / "dist"
    html = (dist / "index.html").read_text(encoding="utf-8")
    snapshot = snapshot_path.read_text(encoding="utf-8")

    # 样式：**离线这一版通常没有独立的 CSS 文件可内联**——
    # IIFE 是单 chunk，vite 会把 CSS 一起打进 JS，运行时注入 `<style>`。
    # 所以这里 `0` 是正常的，不代表漏了。真出现 `<link rel=stylesheet>`
    # （换了构建配置就会）时才内联它，两条路都留着。
    def inline_css(m: re.Match[str]) -> str:
        css = (dist / m.group(1).lstrip("/")).read_text(encoding="utf-8")
        return f"<style>{_safe(css)}</style>"

    html, n_css = re.subn(
        r'<link[^>]+rel="stylesheet"[^>]+href="([^"]+)"[^>]*>', inline_css, html
    )
    print(f"    独立样式表 {n_css} 个"
          + ("（0 正常：CSS 已由 IIFE 打进 JS）" if n_css == 0 else ""))

    # favicon 指向 /favicon.svg，`file://` 下必然 404。删掉——
    # 它不影响任何东西，但会在控制台留一条红字，看的人会以为坏了。
    html = re.sub(r'\s*<link[^>]+rel="icon"[^>]*>', "", html)

    # 脚本：取出 <script src=...>，**挪到 </body> 前**再内联。
    #
    # ⚠ **位置是必须挪的，不是顺手整理。** vite 把这个 script 放在 `<head>`
    #   并带 `type="module"`——模块脚本天然 defer，等文档解析完才执行，
    #   所以那时 `#root` 已经在了。改成**普通**内联脚本之后 defer 就没了：
    #   留在 `<head>` 里会在 `<div id="root">` 出现**之前**执行，
    #   React 找不到挂载点直接抛错，而**页面上只会是一片白**。
    m_js = re.search(r'<script[^>]*\bsrc="([^"]+)"[^>]*></script>', html)
    if m_js is None:
        print("✗ 找不到要内联的脚本标签。多半是 vite 的输出结构变了——"
              "**不要发这个文件**，它打开会是白屏。")
        return 1
    js = (dist / m_js.group(1).lstrip("/")).read_text(encoding="utf-8")
    html = html[: m_js.start()] + html[m_js.end():]

    # 数据放最前面，主脚本一执行就会去读 window.__SNAPSHOT__
    #
    # ⚠ `banner=False` 是给**录视频用**的那一份：录制下来的视频本来就不是
    #   实时演示，「离线快照」那条横幅在那种场景里是多余的（而且会入镜）。
    #   **默认仍然是开**：给会计同学浏览的那一份必须带着它——
    #   他会以为页面上是「现在的库」。**别把默认值改掉。**
    data_tag = "<script>window.__SNAPSHOT__=" + _safe(snapshot) + ";"
    if not banner:
        data_tag += "window.__SNAPSHOT_HIDE_BANNER__=true;"
    data_tag += "</script>\n"
    html = html.replace("<head>", "<head>\n" + data_tag, 1)

    # 兜底自检。**这一条是给「万一」用的**：内联模块脚本在 `file://` 下
    # 各浏览器行为不完全一致，而白屏**不会报错**，会计只会说「打不开」。
    # 有这一段，他至少能拿到一句能转发回来的话。
    guard = (
        "<script>\n"
        "setTimeout(function(){\n"
        "  var r=document.getElementById('root');\n"
        "  if(r && r.childElementCount===0){\n"
        "    document.body.innerHTML=\n"
        "      '<div style=\"font:14px/1.7 sans-serif;padding:24px;max-width:640px\">'\n"
        "      +'<h2>这一页没能打开</h2>'\n"
        "      +'<p>多半是浏览器对「本地文件里的脚本」有限制。请把这一段截图发回群里，'\n"
        "      +'并附上你用的是哪个浏览器（Edge / Chrome / 火狐）和版本。</p>'\n"
        "      +'<p style=\"color:#888\">技术信息：内联脚本未执行，root 为空。</p>'\n"
        "      +'</div>';\n"
        "  }\n"
        "},3000);\n"
        "</script>\n"
    )
    html = html.replace(
        "</body>",
        f'<script>{_safe(js)}</script>\n{guard}</body>',
        1,
    )

    if "id=\"root\"" not in html or "<script>" not in html:
        print("✗ 结构不对（找不到挂载点或脚本没进去）。**不要发这个文件**。")
        return 1

    page = out_dir / "index.html"
    page.write_text(html, encoding="utf-8")
    size = page.stat().st_size
    print(f"✓ {page}（{size / 1024 / 1024:.2f} MB，"
          f"样式 {n_css} 个、脚本已内联到 body 末尾）")

    snapshot_path.unlink()      # 数据已经进了 HTML，别在包里留第二份
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="打一个离线单文件工作台")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument(
        "--no-banner", action="store_true",
        help="不带「离线快照」横幅。**只给录视频用**——那段视频本来就不是"
             "实时演示；给会计浏览的那一份不要加这个参数。",
    )
    args = ap.parse_args()
    return build(Path(args.out), banner=not args.no_banner)


if __name__ == "__main__":
    raise SystemExit(main())

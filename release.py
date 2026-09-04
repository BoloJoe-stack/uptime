#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键发版：读 CHANGELOG 顶部最新版本段 -> 打包 exe -> 打 tag 推送 -> 建 GitHub Release 并挂上 uptime.exe。

用法（在仓库根目录）：
    py -3.10 release.py --build-only   # 只打包，不发布（快速验证）
    py -3.10 release.py --yes          # 跳过确认，直接发布
    py -3.10 release.py                # 发布前打印摘要并询问确认

前置条件（脚本会自己校验）：
    1. 本次代码改动 + CHANGELOG.md 顶部已新增 [vX.Y.Z] 段，且都已 git commit；
       工作区干净（未提交的改动会拦下提示，请先 commit & push）。
    2. 已通过 GitHub 登录 git（Windows Credential Manager / GCM），脚本动态取 token，不落盘。
    3. 网络走 git 已配的代理（127.0.0.1:7897），API 调用会复用该代理。

产物：tag vX.Y.Z + 远端 Release（说明 = CHANGELOG 该版本段，附件 = dist/uptime.exe）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

# 中文输出加固：避免 Windows 管道/GBK 下 print 崩溃
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
CHANGELOG = os.path.join(REPO_ROOT, "CHANGELOG.md")
BUILD_EXE = os.path.join(REPO_ROOT, "build_exe.py")
EXE = os.path.join(REPO_ROOT, "dist", "uptime.exe")
PROXY = "http://127.0.0.1:7897"  # 与 git 仓库级代理一致


def sh(args: list[str], input: str | None = None, check: bool = True,
       timeout: int = 600) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.setdefault("https_proxy", PROXY)
    env.setdefault("http_proxy", PROXY)
    r = subprocess.run(args, cwd=REPO_ROOT, env=env, input=input,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        sys.exit(f"[release] 命令失败: {' '.join(args)}\n{r.stdout}\n{r.stderr}")
    return r


def remote_owner_repo() -> tuple[str, str]:
    r = sh(["git", "remote", "get-url", "origin"], check=False)
    url = r.stdout.strip()
    m = re.search(r"[:/]([^/:]+)/([^/:]+?)(?:\.git)?$", url)
    if not m:
        sys.exit(f"[release] 无法从 origin 解析 owner/repo: {url}")
    return m.group(1), m.group(2)


def parse_latest_version() -> tuple[str, str, str]:
    """返回 (version, date, body)。version 形如 v1.0.0。"""
    with open(CHANGELOG, encoding="utf-8") as f:
        text = f.read()
    m = re.search(r"^##\s+\[(" + r"v\d+\.\d+\.\d+" + r")\]\s*-\s*(\d{4}-\d{2}-\d{2})\s*$",
                  text, re.MULTILINE)
    if not m:
        sys.exit(f"[release] CHANGELOG.md 顶部找不到 '## [vX.Y.Z] - 日期' 版本段，请先新增。")
    version, date = m.group(1), m.group(2)
    after = text[m.end():]
    # 下一个版本段之前的内容即本段正文
    nxt = re.search(r"^##\s+\[", after, re.MULTILINE)
    body = after[: nxt.start()] if nxt else after
    body = body.strip("\n\r -")
    if not body:
        sys.exit(f"[release] {version} 版本段为空，请写更新内容。")
    return version, date, body


def build_exe() -> None:
    print("[release] 打包 exe（py -3.10 build_exe.py，约几分钟）...")
    sh(["py", "-3.10", "build_exe.py"])
    if not os.path.exists(EXE):
        sys.exit(f"[release] 打包产物缺失: {EXE}")
    print(f"[release] 打包完成: {EXE}")


def git_credential_token() -> str:
    """从 GCM 动态取 github.com 凭据（不打印、不落盘）。"""
    r = subprocess.run(["git", "credential", "fill"],
                       input="protocol=https\nhost=github.com\n\n",
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=30)
    for line in (r.stdout or "").splitlines():
        if line.startswith("password="):
            tok = line[len("password="):].strip()
            if tok:
                return tok
    sys.exit("[release] 取不到 GitHub 凭据，请确认已用 git 登录过 github.com（GCM）。")


def api(url: str, *, token: str, method: str = "POST",
        payload: dict | None = None, binary: str | None = None,
        content_type: str | None = None) -> dict | None:
    """调 GitHub REST API。payload 走 JSON；binary 走 raw 上传。"""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        data_file = f.name
        if payload is not None:
            json.dump(payload, f, ensure_ascii=False)
    try:
        args = ["curl", "-sS", "-X", method, url,
                "-H", "Authorization: Bearer " + token,
                "-H", "Accept: application/vnd.github+json"]
        if binary:
            args += ["-H", f"Content-Type: {content_type or 'application/octet-stream'}",
                     "--data-binary", "@" + binary]
        elif payload is not None:
            args += ["-H", "Content-Type: application/json", "--data", "@" + data_file]
        else:
            args.append("--data", "")
        r = sh(args, check=False, timeout=600)
        out = (r.stdout or "").strip()
        if r.returncode != 0:
            sys.exit(f"[release] API {url} 失败: {out} {r.stderr}")
        if not out:
            return None
        return json.loads(out)
    except json.JSONDecodeError:
        sys.exit(f"[release] API {url} 返回非 JSON: {out[:500]}")
    finally:
        os.unlink(data_file)


def main() -> None:
    args = sys.argv[1:]
    build_only = "--build-only" in args
    yes = "--yes" in args

    version, date, body = parse_latest_version()
    print(f"[release] 将发布版本: {version}  ({date})")
    print(f"[release] 更新内容预览:\n{body[:600]}...")

    owner, repo = remote_owner_repo()
    print(f"[release] 目标仓库: {owner}/{repo}")

    tag_exists = sh(["git", "tag", "-l", version], check=False).stdout.strip()
    if tag_exists:
        sys.exit(f"[release] tag {version} 已存在，请先在 CHANGELOG 顶部加新版本段。")

    dirty = sh(["git", "status", "--porcelain"], check=False).stdout.strip()
    if dirty:
        sys.exit("[release] 工作区有未提交改动，请先 git add/commit 代码与 CHANGELOG 改动，再跑本脚本。\n"
                 f"未提交内容:\n{dirty}")

    if build_only:
        build_exe()
        print("[release] --build-only 完成，未发布。")
        return

    print(f"\n[release] 即将：打 tag {version} -> push -> 在 {owner}/{repo} 建 Release "
          f"并上传 uptime.exe（正文 = 上面的更新内容）")
    if not yes:
        if input("确认发布？输入 y 继续，其余退出: ").strip().lower() != "y":
            sys.exit("[release] 已取消。")
    # 确认后先打包（耗时放最后，避免白等）
    build_exe()

    print(f"[release] 打 tag + push ...")
    sh(["git", "tag", "-a", version, "-m", f"uptime {version}"])
    sh(["git", "push", "origin", "HEAD"])
    sh(["git", "push", "origin", version])

    token = git_credential_token()
    rel = api(f"https://api.github.com/repos/{owner}/{repo}/releases",
              token=token,
              payload={"tag_name": version,
                       "name": f"uptime {version}",
                       "body": f"## uptime {version}（{date}）\n\n{body}\n\n---\n完整说明见 [CHANGELOG.md](CHANGELOG.md)。",
                       "draft": False,
                       "prerelease": False})
    rid = rel["id"] if rel else None
    if not rid:
        sys.exit("[release] Release 创建失败（看上面返回）。")
    print(f"[release] Release {version} 已创建 (#{rid})，上传 uptime.exe ...")

    t0 = time.time()
    api(f"https://uploads.github.com/repos/{owner}/{repo}/releases/{rid}/assets"
        f"?name={urllib.parse.quote('uptime.exe')}",
        token=token, binary=EXE, content_type="application/octet-stream")
    print(f"[release] 上传完成，用时 {time.time() - t0:.1f}s")
    print(f"\n✅ 发布成功: https://github.com/{owner}/{repo}/releases/tag/{version}")


if __name__ == "__main__":
    main()

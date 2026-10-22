"""火山方舟接入自检脚本。

用途：确认 API Key 有效、账号已开通哪些模型、免费额度是否生效。
用法：
    python check_ark.py            # 检查当前密钥
    python check_ark.py --set      # 检查并把可用模型写入 config.toml

开通路径：https://console.volcengine.com/ark → 开通管理 → 开通模型
（每个模型开通后送 50 万 token 免费推理额度，安心体验模式下不会产生费用）
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_FILE = ROOT / "secrets" / "ark_api_key.txt"
BASE = "https://ark.cn-beijing.volces.com/api/v3"

# 候选模型：翻译用 lite/mini（额度消耗低），总结用 pro（效果更好）。
# 各模型的可用性因账号而异，以本脚本的实际探测结果为准。
CANDIDATES = [
    ("doubao-seed-2-0-lite-260428", "翻译备选·免费额度足"),
    ("doubao-seed-2-0-mini-260428", "翻译备选·更省额度"),
    ("doubao-seed-2-1-lite-260915", "翻译备选·速度较快"),
    ("doubao-seed-2-1-pro-260915", "总结首选·效果最好"),
    ("doubao-seed-2-1-pro-260628", "总结备选"),
    ("doubao-seed-translation-250915", "官方翻译专用模型"),
]


def _req(url: str, key: str, payload: dict | None = None, timeout: int = 40):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {key}"}
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method="POST" if data else "GET")
    return urllib.request.urlopen(req, timeout=timeout)


def probe_model(key: str, model: str) -> tuple[str, str]:
    """返回 (状态, 说明)。状态 ∈ {可用, 未开通, 已下线, 错误}"""
    try:
        with _req(f"{BASE}/chat/completions", key, {
            "model": model,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 8,
        }) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            txt = str(data["choices"][0]["message"]["content"])[:40]
            return "可用", txt
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)["error"]
            code, msg = err.get("code", ""), err.get("message", "")[:120]
        except Exception:
            code, msg = "?", body[:120]
        if code == "ModelNotOpen":
            return "未开通", msg
        if code == "InvalidEndpointOrModel.NotFound":
            return "已下线", msg
        return "错误", f"[{e.code}] {code}: {msg}"
    except Exception as e:  # noqa: BLE001
        return "错误", f"{e.__class__.__name__}: {e}"


def main() -> int:
    if not KEY_FILE.is_file():
        print(f"[x] 密钥文件不存在：{KEY_FILE}")
        print("    把方舟 API Key 粘贴到该文件（一行一个）后重试。")
        return 2
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    if not key:
        print("[x] 密钥文件为空")
        return 2
    print(f"密钥前缀：{key[:8]}...（长度 {len(key)}）")
    print(f"端点：{BASE}")
    print()

    # 1) 密钥有效性
    try:
        with _req(f"{BASE}/models", key) as resp:
            total = len(json.loads(resp.read().decode("utf-8")).get("data", []))
        print(f"[OK] 密钥有效，模型目录共 {total} 条")
    except urllib.error.HTTPError as e:
        print(f"[x] 密钥无效或无权限：HTTP {e.code}")
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[x] 网络/端点异常：{e}")
        return 1
    print()

    # 2) 逐个模型探测
    usable: list[str] = []
    need_open: list[str] = []
    print("模型开通状态：")
    for model, desc in CANDIDATES:
        status, info = probe_model(key, model)
        mark = {"可用": "[OK]  ", "未开通": "[需开通]", "已下线": "[已下线]",
                "错误": "[错误]"}.get(status, "[?]")
        print(f"  {mark} {model:34s} {desc}")
        if status == "可用":
            usable.append(model)
        elif status == "未开通":
            need_open.append(model)
        if status in ("未开通", "已下线"):
            print(f"         └ {info[:110]}")
    print()

    if not usable:
        print("=" * 62)
        print("当前账号**没有任何已开通的模型**，豆包暂时无法调用。")
        print()
        print("开通步骤（约 2 分钟）：")
        print("  1. 打开 https://console.volcengine.com/ark")
        print("  2. 左侧「开通管理」→ 找到要用的模型 → 点「开通」")
        print("  3. 建议至少开通这两个：")
        print("       doubao-seed-2-0-lite-260428   ← 逐句翻译（额度足、速度快）")
        print("       doubao-seed-2-1-pro-260915    ← 会议总结（质量高）")
        print("  4. 开通即送该模型 50 万 token 免费推理额度")
        print("     （安心体验模式：只扣赠送额度，不会产生意外费用）")
        print()
        print("开通后重跑本脚本：python check_ark.py --set")
        return 1

    print("=" * 62)
    print(f"可用模型 {len(usable)} 个：{', '.join(usable)}")
    if need_open:
        print(f"待开通：{', '.join(need_open)}")

    if "--set" in sys.argv:
        cfg = ROOT / "config.local.toml"
        if not cfg.is_file():
            cfg = ROOT / "config.toml"
        text = cfg.read_text(encoding="utf-8")
        # 翻译链：优先 lite/mini/economy/flash 这类轻量档；总结链：优先 pro/大参数量
        trans = [m for m in usable
                 if any(k in m for k in ("lite", "mini", "economy", "flash", "translation"))]
        summ = [m for m in usable if "pro" in m]
        if not trans:
            trans = list(usable)
        if not summ:
            summ = list(usable)
        # 定位备用后端段（[[translate.alt_backends]]）并重写其 model / fallback_models
        import re

        pat = re.compile(
            r'(\[\[translate\.alt_backends\]\](?:(?!\[\[).)*?model\s*=\s*)"[^"]*"'
            r'((?:(?!\[\[).)*?fallback_models\s*=\s*)\[[^\]]*\]',
            re.S)
        m = pat.search(text)
        if not m:
            print("[x] 未在配置中找到 [[translate.alt_backends]] 段，未做修改")
            return 1
        new_text = (text[:m.start()]
                    + m.group(1) + json.dumps(trans[0], ensure_ascii=False)
                    + m.group(2) + json.dumps(trans, ensure_ascii=False)
                    + text[m.end():])
        new_text = new_text.replace('enabled = false', 'enabled = true', 1)
        cfg.write_text(new_text, encoding="utf-8")
        print(f"\n[OK] 已更新 {cfg.name}：备用后端启用，"
              f"翻译链={trans}，总结链={summ}")
    else:
        print("\n加 --set 参数可自动把可用模型写入配置文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

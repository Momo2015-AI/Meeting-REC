"""全量扫描方舟模型目录，找出账号真正可调用的模型（并发真实 chat 探测）。

用法：python scan_ark_all.py [并发数]
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parent
KEY_FILE = ROOT / "secrets" / "ark_api_key.txt"
BASE = "https://ark.cn-beijing.volces.com/api/v3"


def _call(key: str, model: str) -> tuple[str, str]:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 4,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return "可用", str(data["choices"][0]["message"]["content"])[:30]
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            err = json.loads(body)["error"]
            code, msg = err.get("code", ""), err.get("message", "")[:90]
        except Exception:
            code, msg = "?", body[:90]
        if code == "ModelNotOpen":
            return "未开通", msg
        if code == "InvalidEndpointOrModel.NotFound":
            return "已下线", msg
        if code in ("AccessDenied", "Forbidden"):
            return "无权限", msg
        return "错误", f"{code}: {msg}"
    except Exception as e:  # noqa: BLE001
        return "错误", f"{e.__class__.__name__}: {e}"


def main() -> int:
    key = KEY_FILE.read_text(encoding="utf-8").strip()
    req = urllib.request.Request(f"{BASE}/models",
                                 headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        models = [m.get("id", "") for m in
                  json.loads(resp.read().decode("utf-8")).get("data", [])]
    models = [m for m in models if m]
    print(f"目录共 {len(models)} 个模型，开始全量并发探测…\n")

    workers = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    results: dict[str, tuple[str, str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_call, key, m): m for m in models}
        done = 0
        for fut in as_completed(futs):
            m = futs[fut]
            try:
                results[m] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[m] = ("错误", str(e))
            done += 1
            if done % 20 == 0:
                print(f"  进度 {done}/{len(models)}")

    buckets: dict[str, list[str]] = {}
    for m, (st, info) in results.items():
        buckets.setdefault(st, []).append(m)

    print("\n" + "=" * 64)
    for st in ("可用", "无权限", "未开通", "已下线", "错误"):
        items = buckets.get(st, [])
        if not items:
            continue
        print(f"\n[{st}] {len(items)} 个")
        for m in sorted(items):
            print(f"   {m}   → {results[m][1][:70]}")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

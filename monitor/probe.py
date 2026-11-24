"""诊断闲鱼公开商品页在当前网络环境下究竟返回了什么。

只在排查采集通道时使用：把浏览器实际打开的地址、拦到的 mtop 请求、
页面文本和 cookie 指纹一次性带回来，避免靠猜。
"""

import json

from . import xianyu_product, xy_browser


def trace(item_id, timeout=22000):
    from playwright.sync_api import Error, TimeoutError, sync_playwright

    url = f"https://h5.m.goofish.com/item?id={item_id}"
    calls = []
    console = []
    pages = []

    with sync_playwright() as engine:
        browser = xy_browser.launch(engine)
        try:
            context = xy_browser.new_context(browser, mobile=True)
            page = context.new_page()
            page.on("framenavigated", lambda frame: pages.append(frame.url) if frame == page.main_frame else None)
            page.on("console", lambda message: console.append(f"{message.type}:{message.text[:160]}"))
            page.on("requestfailed", lambda request: calls.append(
                {"kind": "failed", "url": request.url[:160], "why": (request.failure or "")[:80]}))

            def capture(response):
                text = response.url
                if "mtop" not in text and "goofish" not in text:
                    return
                entry = {"kind": "response", "status": response.status, "url": text[:160]}
                try:
                    payload = response.json()
                except Exception:
                    payload = None
                if isinstance(payload, dict):
                    ret = payload.get("ret")
                    entry["ret"] = (ret[0] if isinstance(ret, list) and ret else str(ret))[:120]
                    entry["api"] = payload.get("api")
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    item = data.get("itemDO") if isinstance(data.get("itemDO"), dict) else {}
                    if item:
                        entry["item_keys"] = sorted(item.keys())[:12]
                        entry["wantCnt"] = item.get("wantCnt")
                calls.append(entry)

            page.on("response", capture)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=20000)
            except TimeoutError:
                calls.append({"kind": "goto-timeout"})
            for _ in range(16):
                if any(entry.get("api") == xianyu_product.DETAIL_API and "SUCCESS" in str(entry.get("ret")) for entry in calls):
                    break
                page.wait_for_timeout(500)
            try:
                text = page.locator("body").inner_text(timeout=2000).strip()
            except Exception:
                text = ""
            cookies = [cookie["name"] for cookie in context.cookies()]
            # 两条 appKey 各自能不能过，直接给出结论，省得反复猜。
            attempts = {}
            for app_key in (xianyu_product.PC_APP_KEY, xianyu_product.APP_KEY):
                result = xianyu_product._context_call(context, item_id, app_key)
                attempts[app_key] = "SUCCESS" if isinstance(result, dict) else result[:90]
            return {
                "api_attempts": attempts,
                "proxy": bool(xy_browser.proxy()),
                "webdriver": page.evaluate("navigator.webdriver"),
                "user_agent": page.evaluate("navigator.userAgent")[:120],
                "final_url": page.url[:200],
                "title": (page.title() or "")[:120],
                "body": text[:600],
                "cookies": cookies,
                "navigations": pages[-6:],
                "calls": calls[-24:],
                "console": console[-12:],
            }
        finally:
            browser.close()


if __name__ == "__main__":
    print(json.dumps(trace(sys.argv[1]), ensure_ascii=False, indent=1))

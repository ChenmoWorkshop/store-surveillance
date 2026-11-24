"""Shared Playwright setup for the Xianyu channels.

Datacenter IPs are scored by Alibaba's risk engine, so every detail matters:
the browser must present one consistent identity (UA string matching the
Chromium build and the ``sec-ch-ua`` headers it actually sends), ``webdriver``
must not be exposed, and an operator-supplied egress proxy is honoured when
the service runs behind a flagged IP range.
"""

import os

# 与容器里的 Chromium 版本保持一致，避免 UA 说 Safari、sec-ch-ua 说 Chrome 的破绽。
ANDROID_UA = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
STEALTH = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
"""


def proxy():
    value = (os.getenv("MONITOR_XIANYU_PROXY") or "").strip()
    return {"server": value} if value else None


def channel():
    return os.getenv("MONITOR_BROWSER_CHANNEL") or (
        "msedge" if os.name == "nt" else "chromium")


def launch(engine):
    options = {
        "channel": channel(),
        "timeout": 15000,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    route = proxy()
    if route:
        options["proxy"] = route
    return engine.chromium.launch(**options)


def new_context(browser, mobile=False):
    context = browser.new_context(
        locale="zh-CN",
        user_agent=ANDROID_UA if mobile else DESKTOP_UA,
        viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 900},
        is_mobile=mobile,
        has_touch=mobile,
        extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9"},
    )
    context.add_init_script(STEALTH)
    return context

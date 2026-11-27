"""WorkBuddy Sites 网关下的访问控制适配层。

平台的 CloudStudio 网关会占用 Authorization 头，应用的 Basic Auth 收不到凭据，
所以这里补一层 cookie 登录：登录成功后把 Basic 凭据注入回请求头，仍由
monitor/app.py 原有的鉴权中间件做校验，密码比对规则完全不变。
"""

import base64
import hashlib
import hmac
import os
import secrets
import urllib.parse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, RedirectResponse

COOKIE = "monitor_session"
LOGIN_PATH = "/__login"


def credentials():
    return (
        os.environ.get("MONITOR_ACCESS_USERNAME", "monitor"),
        os.environ.get("MONITOR_ACCESS_PASSWORD", ""),
    )


def token(user, password):
    return hmac.new(
        password.encode("ascii"), f"{user}:{COOKIE}".encode("ascii"), hashlib.sha256
    ).hexdigest()


def page(message=""):
    note = f'<p class="error">{message}</p>' if message else ""
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<meta name="robots" content="noindex,nofollow" />
<title>链接监控台 · 登录</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    background: #0f1115; color: #e6e8ec;
    font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }}
  form {{
    width: min(340px, 88vw); padding: 28px; border-radius: 14px;
    background: #171a21; border: 1px solid #262b35;
    box-shadow: 0 18px 48px rgba(0,0,0,.45);
  }}
  h1 {{ margin: 0 0 4px; font-size: 18px; font-weight: 600; }}
  .sub {{ margin: 0 0 20px; color: #8b93a2; font-size: 13px; }}
  label {{ display: block; margin: 14px 0 6px; font-size: 13px; color: #9aa3b2; }}
  input {{
    width: 100%; padding: 10px 12px; border-radius: 9px; color: #e6e8ec;
    background: #0f1115; border: 1px solid #2b3140; font-size: 15px;
  }}
  input:focus {{ outline: none; border-color: #4b7bec; }}
  button {{
    width: 100%; margin-top: 20px; padding: 11px; border: 0; border-radius: 9px;
    background: #4b7bec; color: #fff; font-size: 15px; font-weight: 600; cursor: pointer;
  }}
  button:hover {{ background: #3f6ade; }}
  .error {{ margin: 12px 0 0; color: #ff7b72; font-size: 13px; }}
</style>
</head>
<body>
<form method="post" action="{LOGIN_PATH}">
  <h1>链接监控台</h1>
  <p class="sub">请输入访问账号</p>
  <label for="u">用户名</label>
  <input id="u" name="u" autocomplete="username" autofocus required />
  <label for="p">密码</label>
  <input id="p" name="p" type="password" autocomplete="current-password" required />
  <button type="submit">进入</button>
  {note}
</form>
</body>
</html>"""


class CookieAuth(BaseHTTPMiddleware):
    """把 cookie 换成 Authorization 头，并给浏览器一个登录表单。"""

    async def dispatch(self, request, call_next):
        user, password = credentials()
        if request.url.path == LOGIN_PATH:
            if request.method == "POST":
                body = await request.body()
                fields = urllib.parse.parse_qs(body.decode("utf-8", "replace"))
                given_user = (fields.get("u") or [""])[0]
                given_password = (fields.get("p") or [""])[0]
                if secrets.compare_digest(given_user, user) and secrets.compare_digest(
                    given_password, password
                ):
                    target = "/"
                    sharecode = request.query_params.get("sharecode")
                    if sharecode:
                        target = "/?sharecode=" + urllib.parse.quote(sharecode)
                    response = RedirectResponse(target, status_code=303)
                    response.set_cookie(
                        COOKIE,
                        token(user, password),
                        max_age=60 * 60 * 24 * 30,
                        httponly=True,
                        secure=True,
                        samesite="lax",
                        path="/",
                    )
                    return response
                return HTMLResponse(page("用户名或密码不对"), status_code=401)
            return HTMLResponse(page())

        cookie = request.cookies.get(COOKIE) or ""
        if password and cookie and secrets.compare_digest(cookie, token(user, password)):
            header = b"Basic " + base64.b64encode(f"{user}:{password}".encode("ascii"))
            scope = request.scope
            scope["headers"] = [
                (key, value) for key, value in scope["headers"] if key.lower() != b"authorization"
            ] + [(b"authorization", header)]

        response = await call_next(request)

        # 仍是 401 说明还没登录：给浏览器登录页，接口保持 401 供脚本判断。
        if response.status_code == 401 and not request.url.path.startswith("/api/"):
            accepts_html = "text/html" in (request.headers.get("accept") or "")
            if accepts_html or request.method in ("GET", "HEAD"):
                return HTMLResponse(page(), status_code=401)
        return response

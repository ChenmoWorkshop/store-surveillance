const cookieName = "__Host-linkwatch";
const sessionSeconds = 7 * 24 * 60 * 60;
const encoder = new TextEncoder();
const hex = value => Array.from(new Uint8Array(value), n => n.toString(16).padStart(2,"0")).join("");
const random = size => hex(crypto.getRandomValues(new Uint8Array(size)));
const constantEqual = (a,b) => {
  if(typeof a!=="string"||typeof b!=="string"||a.length!==b.length)return false;
  let delta=0;
  for(let i=0;i<a.length;i++)delta|=a.charCodeAt(i)^b.charCodeAt(i);
  return delta===0;
};
async function passwordDigest(password,salt){
  const key=await crypto.subtle.importKey("raw",encoder.encode(password),"PBKDF2",false,["deriveBits"]);
  return hex(await crypto.subtle.deriveBits({name:"PBKDF2",salt:encoder.encode(salt),iterations:100000,hash:"SHA-256"},key,256));
}
async function signSession(secret,data){
  const key=await crypto.subtle.importKey("raw",encoder.encode(secret),{name:"HMAC",hash:"SHA-256"},false,["sign"]);
  return hex(await crypto.subtle.sign("HMAC",key,encoder.encode(data)));
}
function locationResponse(location,cookie){
  const headers=new Headers({Location:location,"Cache-Control":"no-store"});
  if(cookie)headers.set("Set-Cookie",cookie);
  return new Response(null,{status:303,headers});
}
const makeCookie=(value,age)=>cookieName+"="+value+"; Path=/; Secure; HttpOnly; SameSite=Strict; Max-Age="+age;
function entryPage(setup=false,message="",status=200,showSetup=false){
  const title=setup?"设置管理员":"管理员登录";
  return new Response("<!doctype html><html lang='zh-CN'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>"+title+" · 链接监控台</title><style>body{font:16px system-ui;color:#182722;background:#f3f6f4;display:grid;place-items:center;min-height:100svh;margin:0}main{background:white;border:1px solid #dce6df;border-radius:16px;width:min(24rem,calc(100vw - 5rem));padding:2rem}h1{font-size:1.6rem;margin:.4rem 0 1.3rem}p{line-height:1.5}label{display:grid;gap:.35rem;margin:1rem 0}input,button{font:inherit;border:1px solid #b6c9bf;border-radius:8px;padding:.7rem}button{width:100%;background:#17644d;color:white;margin-top:.45rem;cursor:pointer}.note{font-size:.9rem;color:#4a5a50}.error{color:#922624}a{color:#17644d}</style><main><p class='note'>链接监控台</p><h1>"+title+"</h1>"+(message?"<p role='alert' class='error'>"+message+"</p>":"")+"<form method='post' action='"+(setup?"/setup":"/login")+"'><label>用户名<input name='username' required autocomplete='username' minlength='3' maxlength='40' "+(setup?"value='alan'":"")+"></label><label>密码<input type='password' name='password' required maxlength='128' autocomplete='"+(setup?"new-password":"current-password")+"' "+(setup?"minlength='12'":"")+"></label>"+(setup?"<p class='note'>密码至少 12 位，建议使用密码管理器生成。</p>":"")+"<button>"+(setup?"保存并登录":"登录")+"</button></form>"+(!showSetup?"":"<p class='note'><a href='/signin-with-chatgpt?return_to=%2Fsetup' target='_top'>首次设置管理员</a></p>")+"</main></html>",
    {status,headers:{"Content-Type":"text/html; charset=utf-8","Cache-Control":"no-store","X-Content-Type-Options":"nosniff",
      "Content-Security-Policy":"default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"}});
}
const trustedOwner = (req,env) => !!env.LINKWATCH_OWNER_EMAIL &&
  !!req.headers.get("oai-authenticated-user-id") &&
  req.headers.get("oai-authenticated-user-email")?.toLowerCase()===env.LINKWATCH_OWNER_EMAIL.toLowerCase();
async function loadAdmin(env){
  if(env.LINKWATCH_MANAGED_ADMIN){
    const admin=JSON.parse(env.LINKWATCH_MANAGED_ADMIN);
    if(typeof admin.username!=="string"||!/^[a-f0-9]{48}$/.test(admin.salt)||
       !/^[a-f0-9]{64}$/.test(admin.digest)||!/^[a-f0-9]{64}$/.test(admin.session_key))
      throw new Error("管理员凭据配置无效");
    return admin;
  }
  return env.DB.prepare("SELECT username,salt,digest,session_key FROM site_admin WHERE id=1").first();
}
async function validSession(req,admin){
  if(!admin)return false;
  const token=(req.headers.get("cookie")||"").split(/;\s*/).find(x=>x.startsWith(cookieName+"="))?.slice(cookieName.length+1);
  const match=token?.match(/^(\d{10}\.[a-f0-9]{32})\.([a-f0-9]{64})$/);
  if(!match||Number(match[1].split(".")[0])<=Math.floor(Date.now()/1000))return false;
  return constantEqual(match[2],await signSession(admin.session_key,match[1]));
}
async function setSession(admin){
  const length=Math.floor(Date.now()/1000)+sessionSeconds;
  const payload=length+"."+random(16);
  return locationResponse("/",makeCookie(payload+"."+await signSession(admin.session_key,payload),sessionSeconds));
}
async function authGate(req,env){
  const u=new URL(req.url),path=u.pathname,method=req.method;
  if(path==="/mcp"){
    if(!trustedOwner(req,env))return json({detail:"需要站点所有者权限"},403);
    return null;
  }
  const db=env.DB;
  if(!db)return json({detail:"数据库尚不可用"},503);
  const admin=await loadAdmin(env);
  if(method!=="GET"&&method!=="HEAD"&&req.headers.get("origin")!==u.origin)
    return json({detail:"请求来源未通过校验"},403);
  if(path==="/setup"){
    if(!trustedOwner(req,env)){
      if(req.headers.has("oai-authenticated-user-id"))return json({detail:"只有站点所有者可以设置管理员"},403);
      return locationResponse("/signin-with-chatgpt?return_to=%2Fsetup");
    }
    if(admin)return locationResponse("/login");
    if(method==="GET")return entryPage(true);
    if(method!=="POST")return json({detail:"请求方法不受支持"},405);
    const form=await req.formData(),username=String(form.get("username")||"").trim(),password=String(form.get("password")||"");
    if(!/^[a-zA-Z0-9_.-]{3,40}$/.test(username)||password.length<12||password.length>128)
      return entryPage(true,"用户名可用字母、数字、点、下划线和连字符。密码需为 12–128 位。",400);
    const salt=random(24),session_key=random(32),digest=await passwordDigest(password,salt);
    const result=await db.prepare("INSERT OR IGNORE INTO site_admin(id,username,salt,digest,session_key) VALUES (1,?,?,?,?)")
      .bind(username,salt,digest,session_key).run();
    if(result.meta?.changes!==1)return locationResponse("/login");
    return setSession({session_key});
  }
  if(path==="/login"){
    if(method==="GET")return entryPage(false,"",200,!admin);
    if(method!=="POST")return json({detail:"请求方法不受支持"},405);
    if(!admin)return entryPage(false,"请先由站点所有者设置管理员。",403,true);
    const form=await req.formData(),username=String(form.get("username")||"").trim(),password=String(form.get("password")||"");
    if(password.length>128||username.length>40)return entryPage(false,"用户名或密码错误。",401);
    const window=Math.floor(Date.now()/600000);
    const ipHash=await signSession(admin.session_key,req.headers.get("cf-connecting-ip")||"unknown");
    const throttle=await db.prepare("INSERT INTO login_attempts(ip_hash,period,attempts) VALUES (?,?,1) ON CONFLICT(ip_hash) DO UPDATE SET attempts=CASE WHEN period=excluded.period THEN attempts+1 ELSE 1 END,period=excluded.period RETURNING attempts")
      .bind(ipHash,window).first();
    if(throttle.attempts>8)return entryPage(false,"尝试次数过多，请稍后再试。",429);
    if(!constantEqual(username,admin.username)||!constantEqual(await passwordDigest(password,admin.salt),admin.digest))
      return entryPage(false,"用户名或密码错误。",401);
    await db.prepare("DELETE FROM login_attempts WHERE ip_hash=? OR period<?").bind(ipHash,window-144).run();
    return setSession(admin);
  }
  if(path==="/logout"&&method==="POST")return locationResponse("/login",makeCookie("",0));
  if(!await validSession(req,admin)){
    if(path.startsWith("/api/"))return json({detail:"请先登录管理员账号"},401);
    return locationResponse("/login");
  }
  return null;
}

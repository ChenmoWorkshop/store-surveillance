import assert from "node:assert/strict";
import {DatabaseSync} from "node:sqlite";
import {readFileSync} from "node:fs";
import worker from "./dist/server/index.js";
const native=new DatabaseSync(":memory:");
native.exec(readFileSync("drizzle/0000_monitor.sql","utf8"));
native.exec(readFileSync("drizzle/0001_admin_auth.sql","utf8"));
class D1Statement {
  constructor(sql,args=[]){this.sql=sql;this.args=args;}
  bind(...args){return new D1Statement(this.sql,args);}
  first(){return native.prepare(this.sql).get(...this.args);}
  all(){return {results:native.prepare(this.sql).all(...this.args)};}
  run(){return {meta:native.prepare(this.sql).run(...this.args)};}
}
const DB={prepare:s=>new D1Statement(s),batch:statements=>statements.map(s=>s.run())};
const environment={DB,LINKWATCH_OWNER_EMAIL:"owner@example.invalid"};
const origin="https://local.invalid";
const owner={"oai-authenticated-user-id":"owner123","oai-authenticated-user-email":environment.LINKWATCH_OWNER_EMAIL};
const form=new URLSearchParams({username:"alan",password:"sample-only-pass-123"});
const visit=(path,options)=>worker.fetch(new Request(origin+path,options),environment);
assert.equal((await visit("/api/v1/targets")).status,401);
assert.equal((await visit("/setup",{headers:{...owner,"oai-authenticated-user-email":"other@example.invalid"}})).status,403);
const setup=await visit("/setup",{method:"POST",headers:{...owner,origin},body:form});
assert.equal(setup.status,303);
assert.match(setup.headers.get("set-cookie"),/Secure; HttpOnly; SameSite=Strict/);
const session=setup.headers.get("set-cookie").split(";")[0];
assert.equal((await visit("/api/v1/targets",{headers:{cookie:session+"0"}})).status,401);
assert.equal((await visit("/api/v1/targets/batch",{method:"POST",headers:{cookie:session,origin:"https://other.invalid"}})).status,403);
assert.equal((await visit("/mcp",{method:"POST"})).status,403);
assert.equal((await visit("/mcp",{method:"POST",headers:owner,body:JSON.stringify({jsonrpc:"2.0",id:1,method:"tools/list"})})).status,200);
assert.equal((await visit("/login",{method:"POST",headers:{origin},body:form})).status,303);
async function call(path,method="GET",body){
  const res=await visit(path,{method,headers:{cookie:session,origin},body:body&&JSON.stringify(body)});
  assert.ok(res.ok,path+" "+res.status+" "+(res.ok?"":await res.text()));
  return res.json();
}
const data=JSON.parse(readFileSync(process.argv[2],"utf8").replace(/^\uFEFF/,""));
for(const [table, rows] of Object.entries(data.tables))
  for(let i=0;i<rows.length;i+=30)await call("/api/v1/admin/restore-batch","POST",{table,rows:rows.slice(i,i+30)});
assert.deepEqual(await call("/api/v1/admin/counts"),data.counts);
assert.ok((await call("/api/v1/targets")).total>0);
assert.ok((await call("/api/v1/targets?q="+encodeURIComponent("真题"))).total>0);
assert.ok((await call("/api/v1/targets/13/products")).products.length>0);
assert.ok((await call("/api/v1/targets/49/series")).items.length>0);
await call("/api/v1/ops/runs");
await call("/api/v1/rules");
console.log("Migration and dashboard routes passed",data.counts);

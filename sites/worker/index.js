const tables = ["targets","import_readings","jobs","snapshots","shop_catalogs","events","deliveries","rules"];
const defaultRules = {hourly_units:5,premium_price_fen:2900,premium_units:2,price_drop_fen:0,state_alerts:false};
const json = (value, status=200) => Response.json(value,{status,headers:{"Cache-Control":"no-store"}});
const fail = (message,status=422) => { throw Object.assign(new Error(message), {status}); };
const stamp = (value = new Date()) => value.toISOString().replace(/\.\d{3}Z$/, "+00:00");
const date = (value) => new Date(new Date(value).valueOf()+28800000).toISOString().slice(0,10);
const cmpTime = (a,b) => (b.observed_at || "").localeCompare(a.observed_at || "") || b.id-a.id;
const currentSlot = () => new Date(Math.floor(Date.now()/3600000)*3600000);
const canonical = (platform,kind,id) => platform === "xianyu"
  ? "https://www.goofish.com/" + (kind==="shop" ? "personal?userId=" : "item?id=") + id
  : "https://www.xiaohongshu.com/" + (kind==="shop" ? "shop/" : "goods-detail/") + id;
const catalogId = (p,i) => p==="xiaohongshu" ? (i.sku_id || i.id) : i.id;
const enabled = (t) => t.enabled && !t.deleted_at;

async function rows(db, table, id) {
  const stmt = id == null ? db.prepare("SELECT data FROM monitor_rows WHERE table_name=?").bind(table)
    : db.prepare("SELECT data FROM monitor_rows WHERE table_name=? AND target_id=?").bind(table,id);
  return (await stmt.all()).results.map(({data})=>JSON.parse(data));
}
async function get(db,table,id) {
  const row=await db.prepare("SELECT data FROM monitor_rows WHERE table_name=? AND id=?").bind(table,String(id)).first();
  return row ? JSON.parse(row.data) : null;
}
function put(db,table,row) {
  const id=row.id ?? row.snapshot_id ?? row.target_id;
  return db.prepare("INSERT INTO monitor_rows(table_name,id,target_id,data) VALUES (?,?,?,?) ON CONFLICT(table_name,id) DO UPDATE SET data=excluded.data")
    .bind(table,String(id),row.target_id ?? (table==="targets" ? row.id : null),JSON.stringify(row));
}
async function nextId(db, table) {
  const r=await db.prepare("SELECT max(CAST(id as INTEGER)) AS maxid FROM monitor_rows WHERE table_name=?").bind(table).first();
  return Math.max((r?.maxid ?? 0)+1, Date.now()*1000+Math.floor(Math.random()*1000));
}
async function target(db,id) {
  const t=await get(db,"targets",id);
  if (!t || t.deleted_at) fail("监控目标不存在",404);
  return t;
}
const parseItems=(c)=>typeof c.items==="string"?JSON.parse(c.items):(c.items || []);
const metric=(t,s)=>t.kind==="shop"?s.shop_active_count:t.platform==="xianyu"?s.want_count:s.exact_sales;
const platformReady=(p,k)=>p==="xiaohongshu";
const countMetric=(t)=>t.kind==="shop"?t.shop_active_count:t.platform==="xianyu"?t.want_count:
  (t.exact_sales ?? (Number((t.raw_sales||"").match(/[\d.]+/)?.[0])*(t.raw_sales?.includes("万")?10000:1) || null));
function observations(snaps) {
  const actual=snaps.filter(x=>x.source!=="catalog");
  return {first_observed_at:actual.at(-1)?.observed_at ?? null,last_observed_at:actual[0]?.observed_at ?? null,
    last_fetched_at:actual.find(x=>["refresh","scheduled"].includes(x.source))?.observed_at ?? null};
}
async function catalogs(db,id,snaps=null) {
  const [history,samples]=await Promise.all([rows(db,"shop_catalogs",id),snaps??rows(db,"snapshots",id)]);
  const indexed=new Map(samples.map(s=>[s.id,s]));
  return history.map(c=>({...c,items:parseItems(c),observed_at:indexed.get(c.snapshot_id)?.observed_at,source:indexed.get(c.snapshot_id)?.source}))
    .sort((a,b)=>b.snapshot_id-a.snapshot_id);
}
function summarize(t, data, latest, jobs, preview) {
  const read=(field)=>data.find(s=>s[field]!=null)?.[field]??null;
  const marked=data.find(s=>["refresh","scheduled"].includes(s.source)&&date(s.observed_at)===date(Date.now())&&s.daily_delta!=null);
  return {...t,...observations(data),configured:platformReady(t.platform,t.kind),observed_at:data[0]?.observed_at,
    raw_sales:read("raw_sales")??preview?.raw_sales??null,exact_sales:read("exact_sales")??preview?.exact_sales??null,
    want_count:read("want_count")??preview?.want_count??null,price_fen:read("price_fen")??preview?.price_fen??null,
    price_label:read("price_label"),listing_state:read("listing_state"),shop_active_count:latest?.active_count??null,
    daily_delta:marked?.daily_delta??0,hourly_delta:marked?.hourly_delta??0,
    job_state:jobs[0]?.state,job_error:jobs[0]?.error,job_slot:jobs[0]?.slot};
}
async function scope(db) {
  const [targets,snaps,cats,jobs,imports]=await Promise.all(tables.slice(0,5).map(name=>rows(db,name)))
    .then(([targets,imports,jobs,snaps,cats])=>[targets,snaps,cats,jobs,imports]);
  const by=(data,key)=>{const result=new Map();for(const row of data){const id=row[key];if(!result.has(id))result.set(id,[]);result.get(id).push(row);}return result;};
  const samples=by(snaps,"target_id");
  const catalogMap=by(cats,"target_id");
  const jobMap=by(jobs,"target_id");
  const previewMap=new Map(imports.map(r=>[r.target_id,JSON.parse(r.metrics)]));
  return {targets:targets.filter(t=>!t.deleted_at),samples,catalogMap,jobMap,previewMap};
}
async function listTargets(db,params) {
  const {targets,samples,catalogMap,jobMap,previewMap}=await scope(db);
  const platform=params.get("platform"),kind=params.get("kind"),sort=params.get("sort")??"";
  const words=(params.get("q")??"").toLowerCase().split(/\s+/).filter(Boolean);
  const match=(...values)=>words.every(w=>values.join(" ").toLowerCase().includes(w));
  const keys=new Set(), result=[];
  const key=(p,k,id)=>p+":"+k+":"+id;
  const lastCatalog=(id)=>catalogMap.get(id)?.reduce((a,b)=>a.snapshot_id>b.snapshot_id?a:b);
  const summary=(t)=>summarize(t,(samples.get(t.id)||[]).sort(cmpTime),lastCatalog(t.id),
    (jobMap.get(t.id)||[]).sort((a,b)=>b.id-a.id),previewMap.get(t.id));
  for(const t of targets) {
    if ((!platform||t.platform===platform)&&(!kind||t.kind===kind)&&
        (!t.shop_only||words.length)&&match(t.title,t.official_title,t.external_id,t.kind==="shop"?t.shop_name:"")) {
      result.push(summary(t));keys.add(key(t.platform,t.kind,t.external_id));
    }
  }
  if(words.length&&kind!=="shop") for(const shop of targets.filter(t=>t.kind==="shop"&&(!platform||t.platform===platform))) {
    const found=parseItems(lastCatalog(shop.id)??{});
    for(const i of found.filter(i=>match(i.title,i.id,i.sku_id))) {
      const external_id=catalogId(shop.platform,i), k=key(shop.platform,"product",external_id);
      const t=targets.find(row=>row.kind==="product"&&row.platform===shop.platform&&[i.id,external_id].includes(row.external_id));
      const matching=result.find(row=>row.kind==="product"&&row.platform===shop.platform&&[i.id,external_id].includes(row.external_id));
      if(matching){matching.search_shop_id??=shop.id;matching.shop_name??=shop.shop_name||shop.title;continue;}
      if(keys.has(k))continue;
      keys.add(k);
      result.push({...i,...(t?summary(t):{}),id:t?.id??null,title:i.title,platform:shop.platform,kind:"product",external_id,
        url:canonical(shop.platform,"product",external_id),catalog_match:true,monitoring:!!(t&&enabled(t)),
        search_shop_id:shop.id,shop_name:shop.shop_name||shop.title,
        price_fen:(t&&summary(t).price_fen)??i.price_fen,raw_sales:(t&&summary(t).raw_sales)??i.raw_sales});
    }
  }
  const field=sort.replace(/_asc$/,"");
  const val=t=>field==="price"?t.price_fen:field==="metric"?countMetric(t):field==="hourly"?t.hourly_delta??0:
    field==="daily"?t.daily_delta??0:t.id;
  result.sort((a,b)=>(a.kind==="shop")-(b.kind==="shop") || (val(a)==null)-(val(b)==null) ||
    ((val(a)??0)-(val(b)??0))*(sort.endsWith("_asc")?1:-1) || (b.id??0)-(a.id??0));
  const offset=Math.max(0,Number(params.get("offset"))||0), limit=Math.min(200,Number(params.get("limit"))||50);
  return {items:result.slice(offset,offset+limit),total:result.length};
}
async function series(db,t,limit) {
  const [history,jobRows,previewRecord,catRows,allTargets]=await Promise.all([
    rows(db,"snapshots",t.id),rows(db,"jobs",t.id),get(db,"import_readings",t.id),rows(db,"shop_catalogs"),rows(db,"targets")]);
  const snaps=history.sort(cmpTime), storeCats=catRows.filter(c=>c.target_id===t.id);
  const onShelf=new Map(storeCats.map(c=>[c.snapshot_id,c.active_count]));
  const entries=snaps.map(s=>({...s,shop_active_count:onShelf.get(s.id)??null}));
  const bySlot=new Map(entries.filter(s=>s.slot).map(s=>[s.slot,s]));
  const hours=jobRows.sort((a,b)=>b.slot.localeCompare(a.slot)).map(j=>({...bySlot.get(j.slot),...j}));
  const seen=new Map();
  for(const s of entries) if(s.source!=="catalog"&&(metric(t,s)!=null||s.raw_sales)) {
    const day=date(s.observed_at);
    if(!seen.has(day))seen.set(day,{...s,day,value:metric(t,s),display:s.raw_sales});
  }
  const days=[...seen.values()];
  for (const [i,d] of days.entries()){
    const p=days[i+1];
    d.delta=p?.value!=null&&d.value!=null&&Date.parse(d.day)-Date.parse(p.day)===86400000?d.value-p.value:null;
  }
  const observation=observations(entries);
  const preview=previewRecord?{...JSON.parse(previewRecord.metrics),observed_at:previewRecord.observed_at}:null;
  if(preview && (!observation.last_fetched_at||preview.observed_at>observation.last_fetched_at))observation.last_fetched_at=preview.observed_at;
  if(t.kind==="product"&&(!t.listed_at||!t.shop_name)){
    const shops=new Map(allTargets.filter(a=>a.kind==="shop"&&a.platform===t.platform&&!a.deleted_at).map(a=>[a.id,a]));
    const matching=catRows.sort((a,b)=>b.snapshot_id-a.snapshot_id).find(c=>shops.has(c.target_id)&&
      parseItems(c).some(i=>[i.id,catalogId(t.platform,i)].includes(t.external_id)));
    if(matching){
      const item=parseItems(matching).find(i=>[i.id,catalogId(t.platform,i)].includes(t.external_id));
      const shop=shops.get(matching.target_id);
      t={...t,listed_at:t.listed_at||item.listed_at,shop_name:t.shop_name||shop.shop_name||shop.title};
    }
  }
  const trio=hours.slice(0,3);
  const three=trio.length===3&&trio.every((j,i)=>j.state==="success"&&j.hourly_delta!=null&&
    (!i||Date.parse(trio[i-1].slot)-Date.parse(j.slot)===3600000))?trio.reduce((n,j)=>n+j.hourly_delta,0):null;
  return {target:t,items:entries.slice(0,limit),catalogs:await catalogs(db,t.id,entries),preview,observation,
    hours:hours.slice(0,limit),three_hour_delta:three,categories:[...new Set(allTargets.map(t=>t.category).filter(Boolean))],days};
}
async function shopProducts(db,shop,params) {
  if(shop.kind!=="shop")fail("没有店铺商品子页",404);
  const [storeCatalogs,history,allTargets,allSamples]=await Promise.all([catalogs(db,shop.id),rows(db,"snapshots",shop.id),rows(db,"targets"),rows(db,"snapshots")]);
  const latest=storeCatalogs[0], size=Math.min(100,Math.max(1,Number(params.get("page_size"))||20));
  const total=latest?.items.length??0,page_count=Math.max(1,Math.ceil(total/size));
  const page=Math.min(page_count,Math.max(1,Number(params.get("page"))||1)),offset=(page-1)*size;
  const products=(latest?.items??[]).slice(offset,offset+size).map(i=>{
    const external_id=catalogId(shop.platform,i);
    const t=allTargets.find(t=>t.kind==="product"&&t.platform===shop.platform&&!t.deleted_at&&[i.id,external_id].includes(t.external_id));
    const samples=t?allSamples.filter(s=>s.target_id===t.id).sort(cmpTime):[];
    const price=samples.find(s=>s.price_fen!=null),sale=samples.find(s=>s.raw_sales!=null||s.exact_sales!=null),want=samples.find(s=>s.want_count!=null);
    const change=samples.find(s=>date(s.observed_at)===date(Date.now())&&s.daily_delta!=null);
    return {...i,external_id,url:canonical(shop.platform,"product",external_id),target_id:t?.id??null,monitoring:!!(t&&enabled(t)),
      listed_at:t?.listed_at||i.listed_at,price_fen:price?.price_fen??i.price_fen,
      price_observed_at:price?.observed_at??latest.observed_at,price_source:price?"product":"catalog",
      raw_sales:sale?.raw_sales??sale?.exact_sales??i.raw_sales,sales_observed_at:sale?.observed_at??latest.observed_at,
      want_count:want?.want_count??i.want_count,want_observed_at:want?.observed_at,
      hourly_delta:change?.hourly_delta??0,daily_delta:change?.daily_delta??0};
  });
  const summary=latest?{...latest,items:undefined,complete:!latest.has_more&&total===latest.active_count,
    latest_listed_at:latest.items.map(i=>i.listed_at).filter(Boolean).sort().at(-1)}:null;
  const previous=storeCatalogs[1];
  return {target:shop,latest:summary,observation:observations(history.sort(cmpTime)),
    comparison:latest&&previous?{active_change:latest.active_count-previous.active_count}:null,
    products,pagination:{page,page_size:size,page_count,total,offset}};
}

function safeUrl(input) {
  const u=new URL(input);
  if(u.protocol!=="https:"||u.port||u.username||u.password||
    !["www.xiaohongshu.com","xiaohongshu.com","mall.xiaohongshu.com","xhslink.com","www.xhslink.com",
      "www.goofish.com","h5.m.goofish.com","pages.goofish.com","m.tb.cn"].includes(u.hostname))fail("链接域名不受支持");
  return u;
}
async function resolve(input) {
  let u=safeUrl(input);
  for(let step=0;step<5;step++) {
    if(!["m.tb.cn","xhslink.com","www.xhslink.com"].includes(u.hostname))break;
    const response=await fetch(u,{redirect:"manual",signal:AbortSignal.timeout(6000)});
    if(response.status>=300&&response.status<400&&response.headers.has("location")){
      u=safeUrl(new URL(response.headers.get("location"),u));continue;
    }
    const html=await response.text();
    const match=html.match(/https:\/\/(?:h5\.m|www|pages)\.goofish\.com\/[^"'\s<>\\]+/);
    if(!match)fail("短链暂未展开，请粘贴商品或店铺长链接");
    u=safeUrl(match[0].replaceAll("&amp;","&"));
  }
  const platform=u.hostname.includes("goofish")?"xianyu":"xiaohongshu";
  let kind,id;
  if(platform==="xianyu"){
    kind=u.pathname==="/item"?"product":"shop";
    id=kind==="product"?(u.searchParams.get("id")||u.searchParams.get("itemId")):
      (u.searchParams.get("userId")||u.searchParams.get("userid")||u.searchParams.get("sellerId"));
  }else{
    const match=u.pathname.match(/^\/(shop|goods-detail)\/([a-f0-9]{24})\/?$/i);
    kind=match?.[1]==="shop"?"shop":"product";id=match?.[2];
  }
  if(!id||!(platform==="xianyu"?/^\d{5,24}$/:/^[a-f0-9]{24}$/i).test(id))fail("未识别到店铺或商品 ID");
  return {platform,kind,external_id:id,url:canonical(platform,kind,id)};
}
function xhsPrice(text) {
  const n=String(text??"").trim().replace(/^¥\s*/,"");
  return /^\d{1,9}(?:\.\d{1,2})?$/.test(n)?Math.round(Number(n)*100):undefined;
}
function dateFromEpoch(value) {
  const n=Number(value);
  if(!n||!Number.isFinite(n))return null;
  const dateValue=new Date(n>1e12?n:n*1000);
  return Number.isFinite(dateValue.valueOf())&&dateValue.valueOf()<=Date.now()?stamp(dateValue):null;
}
function salesMetric(text) {
  if(typeof text!=="string")return {};
  text=text.trim();
  if(!/^已售\s*[\d,.]+万?\+?(?:件|单)?$/.test(text))return {};
  const exact=text.replaceAll(",","").match(/^已售\s*(\d+)(?:件|单)?$/);
  return {raw_sales:text,exact_sales:exact?Number(exact[1]):null};
}
async function xhsProduct(t) {
  const u=new URL("https://mall.xiaohongshu.com/api/store/jpd/edith/detail/h5/toc");
  u.search=new URLSearchParams({version:"0.0.5",item_id:t.external_id});
  const response=await fetch(u,{redirect:"manual",signal:AbortSignal.timeout(7000)});
  if(!response.ok)fail("小红书商品数据源返回 HTTP "+response.status,502);
  const payload=await response.json();
  const p=payload.data?.template_data?.[0];
  const state=(s)=>/^(?:该|此)?商品已下架[。！]?$/.test(s)?"delisted":
    /^(?:该|此)?商品因违规(?:已被|被|已)?(?:删除|移除|下架)[。！]?$/.test(s)?"removed_violation":
    /^(?:该|此)?商品不存在[。！]?$/.test(s)?"unavailable":null;
  if(!payload.success&&state(payload.msg))return {listing_state:state(payload.msg)};
  if(!payload.success||![p?.descriptionH5?.skuId,p?.descriptionMain?.itemId].includes(t.external_id))
    fail("平台未返回可核对的小红书商品数据",502);
  const price=p.priceH5||{},deal=price.dealPrice||{};
  const tagged=["到手价","券后价"].includes(deal.priceText);
  const actions=p.bottomBarMainH5?.buttons??[];
  const action=actions.some(b=>b.type==="instantBuy"&&b.actionType==="instantBuy"&&!b.disabled);
  const sold=actions.some(b=>/售罄|已抢光/.test(b.text));
  const result={...salesMetric([price.spuAnalysisDataText,p.descriptionMain?.spuAnalysisDataText,
    price.itemAnalysisDataText,p.descriptionH5?.itemAnalysisDataText].find(x=>typeof x==="string"&&x.startsWith("已售"))),
    price_fen:xhsPrice(tagged?deal.price:price.highlightPrice),price_label:tagged?deal.priceText:null,
    official_title:p.descriptionH5?.name||p.descriptionMain?.name,shop_name:p.sellerH5?.name,
    listed_at:dateFromEpoch(p.descriptionH5?.itemOnShelfTime||p.descriptionMain?.itemOnShelfTime),
    listing_state:sold?"sold":action?"active":null};
  const cover=p.carouselH5?.images?.[0]?.url;
  if(typeof cover==="string"&&/^https:\/\/[a-z\d.-]+\.xhscdn\.com\//i.test(cover))result.cover_url=cover;
  if(result.price_fen==null&&!result.raw_sales&&!result.listing_state)fail("小红书未返回读数",502);
  return result;
}
function xhsCards(c,shop) {
  const data=c?.componentData??c?.component_data??{};
  return (data.componentVOList??data.component_vo_list??[]).flatMap(row=>{
    const goods=row.defaultGoodsComponentVO??row.default_goods_component_vo;
    const p=goods?.baseInfo??goods?.base_info;
    if(!p||(p.sellerId??p.seller_id)!==shop||!p.title||!/^[a-f0-9]{24}$/.test(p.itemId??p.item_id??""))return [];
    const marketing=goods.marketingInfo??goods.marketing_info??{};
    const prices=marketing.priceInfo??marketing.price_info??{};
    const expected=prices.expectedPrice??prices.expected_price??{};
    const tags=goods.tagStrategyMap??goods.tag_strategy_map??{};
    const values=tags.afterPrice??tags.after_price??[];
    const raw_sales=values.map(v=>v.tagName??v.tag_name??v.text).find(v=>typeof v==="string"&&v.startsWith("已售"));
    return [{id:p.itemId??p.item_id,sku_id:p.skuId??p.sku_id,title:p.title,price_fen:xhsPrice(expected.priceStr??expected.price_str),
      raw_sales,listed_at:dateFromEpoch(p.itemOnShelfTime??p.item_on_shelf_time)}];
  });
}
async function xhsShop(t) {
  const r=await fetch(t.url,{redirect:"manual",signal:AbortSignal.timeout(7000)});
  if(!r.ok)fail("小红书公开店铺页返回 HTTP "+r.status,502);
  const html=await r.text();
  const start=html.match(/window\.__SETUP_SERVER_STATE__\s*=\s*/);
  if(!start)fail("小红书未返回公开店铺清单",502);
  const tail=html.slice(start.index+start[0].length).split("</script>")[0].trim();
  let state;
  try {state=JSON.parse(tail.replace(/;\s*$/,""));}
  catch {state=JSON.parse(tail.slice(0,tail.lastIndexOf("}")+1));}
  const sections=state.xhs_shop_home_data??[];
  const section=sections.find(x=>x.componentName==="DefaultMixCardComponent");
  const total=section?.componentData?.total;
  if(!Number.isInteger(total))fail("小红书未返回店铺在售数",502);
  const store=sections.find(x=>x.componentName==="DefaultShopEntranceComponent")?.componentData?.componentVOList
    ?.find(x=>x.baseSellerInfo?.sellerId===t.external_id);
  const items=new Map(xhsCards(section,t.external_id).map(i=>[i.id,i]));
  for(let page=0;items.size<total&&page<20;page++){
    const response=await fetch("https://mall.xiaohongshu.com/api/store/guide/components/general/h5",{
      method:"POST",headers:{"Content-Type":"application/json","Accept":"application/json"},
      body:JSON.stringify({biz_identity:{template_id:"shopH5Page",biz_type:"shop",scene:"shop_pc"},
        component_param:{mix_card_component_param:[{sort_type:"sales_qty",filter_param:"",page_param:{page,page_size:10}}]},
        common_param:{seller_id:t.external_id}}),signal:AbortSignal.timeout(5000)});
    if(!response.ok)break;
    const result=await response.json();
    const components=result.data?.components;
    const next=components?.find?.(x=>(x.componentName??x.component_name)==="DefaultMixCardComponent");
    const cards=xhsCards(next,t.external_id);
    if(!cards.length)break;
    for(const i of cards)items.set(i.id,i);
  }
  return {shop_name:store?.baseSellerInfo?.sellerName,follower_count:store?.userInfo?.fansCnt,
    shop_catalog:{active_count:total,items:[...items.values()],has_more:items.size!==total}};
}
async function readCurrent(t) {
  if(t.platform==="xianyu")fail("闲鱼自动采样尚未接入，现有历史和手工录入可用",409);
  return t.kind==="shop"?xhsShop(t):xhsProduct(t);
}
async function record(db,t,reading,source,slot) {
  const history=(await rows(db,"snapshots",t.id)).sort(cmpTime);
  const observed_at=stamp();
  const value=t.kind==="shop"?reading.shop_catalog?.active_count:t.platform==="xianyu"?reading.want_count:reading.exact_sales;
  const oldCatalogs=t.kind==="shop"?await catalogs(db,t.id,history):[];
  const candidates=history.filter(s=>["scheduled","refresh"].includes(s.source)&&s.observed_at>=t.active_since)
    .map(s=>({...s,value:t.kind==="shop"?oldCatalogs.find(c=>c.snapshot_id===s.id)?.active_count:metric(t,s)}))
    .filter(s=>s.value!=null);
  const previous=candidates[0], before=slot?candidates.find(s=>s.source==="scheduled"&&s.slot<slot):previous;
  const reset=t.platform==="xiaohongshu"&&t.kind==="product"&&previous?.value>value;
  const dayBase=previous?.daily_baseline_at;
  const change=previous?.value!=null?value-previous.value:0;
  const auto=source==="scheduled"||source==="refresh";
  const daily_delta=auto&&value!=null?(reset||!dayBase||date(dayBase)!==date(observed_at)?0:(previous.daily_delta??0)+change):null;
  const withinHour=before&&(!slot||Date.parse(slot)-Date.parse(before.slot)<=21600000);
  const hourly_delta=auto&&value!=null&&withinHour&&!reset?value-before.value:auto&&value!=null?0:null;
  const id=await nextId(db,"snapshots");
  const s={id,target_id:t.id,slot:slot??null,observed_at,source,raw_sales:reading.raw_sales??null,
    exact_sales:reading.exact_sales??null,price_fen:reading.price_fen??null,want_count:reading.want_count??null,
    price_label:reading.price_label??null,listing_state:reading.listing_state??null,
    hourly_delta,daily_delta,daily_baseline_at:daily_delta==null?null:daily_delta===0?(dayBase&&date(dayBase)===date(observed_at)&&!reset?dayBase:observed_at):dayBase,
    quality:reset?"counter_reset":reading.exact_sales!=null?"exact":reading.raw_sales?"display_only":"unavailable"};
  for(const key of ["official_title","shop_name","listed_at","follower_count","cover_url"])
    if(reading[key]!=null)t[key]=reading[key];
  const commands=[put(db,"snapshots",s),put(db,"targets",t)];
  if(reading.shop_catalog)commands.push(put(db,"shop_catalogs",{snapshot_id:id,target_id:t.id,...reading.shop_catalog,
    items:JSON.stringify(reading.shop_catalog.items)}));
  if(slot&&source==="scheduled"&&t.platform==="xiaohongshu"&&t.kind==="product"&&before&&
    Date.parse(slot)-Date.parse(before.slot)===3600000&&hourly_delta>0){
    const rules=await getRules(db);
    const matched=[];
    if(hourly_delta>=rules.hourly_units)matched.push("小时销量");
    if(reading.price_fen>=rules.premium_price_fen&&hourly_delta>=rules.premium_units)matched.push("高客单价");
    if(matched.length)commands.push(put(db,"events",{id,target_id:t.id,snapshot_id:id,created_at:observed_at,
      summary:"近一小时 +"+hourly_delta+" · 今日 "+daily_delta,matched:JSON.stringify(matched),notify:0}));
  }
  await db.batch(commands);
  return id;
}
async function getRules(db){
  const old=await get(db,"rules",1);
  return {...defaultRules,...(old?.settings?JSON.parse(old.settings):{})};
}
async function sampleBatch(db,cap=12) {
  const slot=stamp(currentSlot());
  const [targets,jobs]=await Promise.all([rows(db,"targets"),rows(db,"jobs")]);
  const done=new Set(jobs.filter(j=>j.slot===slot).map(j=>j.target_id));
  const eligible=targets.filter(t=>t.platform==="xiaohongshu"&&enabled(t)&&t.active_since<=slot&&!done.has(t.id))
    .sort((a,b)=>(a.kind==="product")-(b.kind==="product")||a.id-b.id);
  let success=0,failed=0;
  // Shop refresh can page through multiple public requests. Keep it in a separate batch.
  const batch=eligible[0]?.kind==="shop"?eligible.slice(0,1):eligible.slice(0,cap);
  for(const t of batch){
    const started=stamp();
    const j={id:await nextId(db,"jobs"),target_id:t.id,slot,state:"queued",attempts:0,next_at:started,error:null};
    try{
      const existing=(await rows(db,"snapshots",t.id)).find(s=>s.slot===slot);
      if(!existing)await record(db,t,await readCurrent(t),"scheduled",slot);
      j.state="success";success++;
    }catch(e){j.state="failed";j.error=String(e.message).slice(0,300);j.attempts=1;failed++;}
    await put(db,"jobs",j).run();
  }
  return {slot,success,failed,remaining:eligible.length-batch.length};
}
async function addLinks(db,body) {
  const inputs=[...new Set((String(body.text??"").replaceAll("\\: ",":").replaceAll("\\:",":")
    .match(/https:\/\/[^\s，。】」）"'<>\u005c]+/g)||[]))];
  if(!inputs.length||inputs.length>200)fail("请输入 1–200 条 HTTPS 分享链接");
  const all=await rows(db,"targets"), out=[];
  for(const url of inputs){
    try{
      const resolved=await resolve(url);
      if(body.kind&&resolved.kind!==body.kind)fail("请用对应的商品或店铺入口");
      const old=all.find(t=>t.platform===resolved.platform&&t.kind===resolved.kind&&t.external_id===resolved.external_id);
      const fallback=(resolved.kind==="shop"?"店铺 ":"商品 ")+resolved.external_id;
      const t=old?{...old,enabled:old.deleted_at?1:old.enabled,deleted_at:null,shop_only:0}:
        {...resolved,id:await nextId(db,"targets"),title:fallback,active_since:stamp(),enabled:1,shop_only:0};
      await put(db,"targets",t).run();
      if(!old)all.push(t);
      out.push({url,...t,resolved_url:resolved.url,status:old?(old.deleted_at?"restored":"existing"):"created",
        title_source:old?"saved":"unavailable"});
    }catch(error){out.push({url,status:"rejected",reason:error.message});}
  }
  return {items:out};
}
async function changeMonitoring(db,shop,body){
  if(shop.kind!=="shop")fail("没有店铺商品子页",404);
  let items=(await catalogs(db,shop.id))[0]?.items??[];
  if(body.item_id!=null)items=items.filter(i=>i.id===String(body.item_id));
  const all=await rows(db,"targets");
  let changed=0,skipped=0;
  for(const i of items){
    const external_id=catalogId(shop.platform,i);
    const t=all.find(x=>x.kind==="product"&&x.platform===shop.platform&&[i.id,external_id].includes(x.external_id));
    if(body.enabled&&shop.platform==="xiaohongshu"&&!i.sku_id&&!t){skipped++;continue;}
    if(!body.enabled&&!t)continue;
    if(t&&!t.deleted_at&&Boolean(t.enabled)===Boolean(body.enabled))continue;
    const change=t?{...t,enabled:body.enabled?1:0,...(body.enabled?{active_since:stamp(),deleted_at:null}:{})}:
      {id:await nextId(db,"targets"),kind:"product",platform:shop.platform,external_id,url:canonical(shop.platform,"product",external_id),
        title:i.title,listed_at:i.listed_at??null,shop_name:shop.shop_name||shop.title,shop_only:1,enabled:1,active_since:stamp()};
    await put(db,"targets",change).run();changed++;
  }
  return {changed,skipped};
}
const csv=(lines,name)=>new Response("\uFEFF"+lines.map(row=>row.map(v=>{
  const text=String(v??"");return '"'+(/^[=+\-@\t\r]/.test(text)?"'":"")+text.replaceAll('"','""')+'"';
}).join(",")).join("\r\n"),{headers:{"Content-Type":"text/csv; charset=utf-8","Content-Disposition":"attachment; filename="+name}});

async function migrate(db,body){
  const table=body.table;
  if(!tables.includes(table)||!Array.isArray(body.rows)||!body.rows.length||body.rows.length>50)fail("迁移请求格式不正确");
  for(const item of body.rows){
    if(!item||!Number.isSafeInteger(item.id??item.snapshot_id??item.target_id))fail("迁移记录缺少 ID");
  }
  await db.batch(body.rows.map(r=>put(db,table,r)));
  return {table,restored:body.rows.length};
}
async function counts(db){
  const result=await db.prepare("SELECT table_name,count(*) AS total FROM monitor_rows GROUP BY table_name").all();
  return Object.fromEntries(tables.map(t=>[t,result.results.find(r=>r.table_name===t)?.total??0]));
}
async function mcp(db,body){
  const {id,method,params}=body;
  let result;
  if(method==="server/discover")result={supportedVersions:["2026-07-28"],capabilities:{tools:{}}};
  else if(method==="initialize")result={protocolVersion:params?.protocolVersion??"2025-11-25",capabilities:{tools:{}},
    serverInfo:{name:"linkwatch",version:"1.0.0"}};
  else if(method==="tools/list")result={tools:[
    {name:"sample_xhs_batch",description:"采集当前整点尚未处理的一批小红书目标，并写入监控历史。返回完成数、失败数和剩余数。",
      inputSchema:{type:"object",properties:{},additionalProperties:false},annotations:{readOnlyHint:false,idempotentHint:true}},
    {name:"monitor_status",description:"只读检查站点和迁移历史记录的数量。",
      inputSchema:{type:"object",properties:{},additionalProperties:false},annotations:{readOnlyHint:true}}]};
  else if(method==="tools/call"){
    const value=params?.name==="sample_xhs_batch"?await sampleBatch(db,6):params?.name==="monitor_status"?await counts(db):null;
    if(!value)return json({jsonrpc:"2.0",id,error:{code:-32601,message:"Unknown tool"}});
    result={content:[{type:"text",text:JSON.stringify(value)}]};
  }else if(id==null)return new Response(null,{status:202});
  else return json({jsonrpc:"2.0",id,error:{code:-32601,message:"Unknown method"}});
  return json({jsonrpc:"2.0",id,result});
}
async function handle(req,env){
  const u=new URL(req.url),method=req.method,path=u.pathname;
  if(path in assets&&(method==="GET"||method==="HEAD")){
    const a=assets[path];
    return new Response(method==="HEAD"?null:Uint8Array.from(atob(a.base64),x=>x.charCodeAt(0)),{
      headers:{"Content-Type":a.mime,"Cache-Control":"no-store","X-Content-Type-Options":"nosniff"}});
  }
  const db=env.DB;
  if(!db)fail("数据库尚不可用",503);
  if(path==="/mcp"&&method==="POST")return mcp(db,await req.json());
  if(path==="/healthz")return json({ok:true});
  if(path==="/api/v1/admin/counts"&&method==="GET")return json(await counts(db));
  if(path==="/api/v1/admin/export"&&method==="GET"){
    const records=await Promise.all(tables.map(table=>rows(db,table)));
    return json({version:1,exported_at:stamp(),tables:Object.fromEntries(tables.map((t,i)=>[t,records[i]])),
      counts:Object.fromEntries(tables.map((t,i)=>[t,records[i].length]))});
  }
  if(path==="/api/v1/admin/restore-batch"&&method==="POST")return json(await migrate(db,await req.json()));
  if(path==="/api/v1/admin/sample-batch"&&method==="POST")return json(await sampleBatch(db,6));
  if(path==="/api/v1/health")return json({sources:{xiaohongshu:true,xianyu:false},
    source_kinds:{xiaohongshu:["shop","product"],xianyu:[]},public_shop:"xiaohongshu"});
  if(path==="/api/v1/targets"&&method==="GET")return json(await listTargets(db,u.searchParams));
  if(path==="/api/v1/targets/batch"&&method==="POST")return json(await addLinks(db,await req.json()));
  if(path==="/api/v1/rules"){
    if(method==="GET")return json(await getRules(db));
    if(method==="PUT"){
      const rule=await req.json();
      for(const key of ["hourly_units","premium_price_fen","premium_units","price_drop_fen"])
        if(!Number.isSafeInteger(rule[key])||rule[key]<0)fail("提醒阈值无效");
      await put(db,"rules",{id:1,settings:JSON.stringify(rule)}).run();
      return json(rule);
    }
  }
  if(path==="/api/v1/events"||path==="/api/v1/ops/runs"){
    const byId=new Map((await rows(db,"targets")).map(t=>[t.id,t]));
    const result=async(name)=>(await rows(db,name)).filter(r=>name==="deliveries"||!!byId.get(r.target_id))
      .sort((a,b)=>b.id-a.id).slice(0,100).map(r=>({...r,...(name==="deliveries"?{}:{
        platform:byId.get(r.target_id).platform,title:byId.get(r.target_id).title}),
        ...(name==="events"?{matched:JSON.parse(r.matched||"[]")}:{})}));
    if(path.endsWith("events"))return json({items:await result("events")});
    return json({jobs:await result("jobs"),deliveries:await result("deliveries")});
  }
  const match=path.match(/^\/api\/v1\/targets\/(\d+)(?:\/(.*))?$/);
  if(!match)fail("页面不存在",404);
  const t=await target(db,Number(match[1])), action=match[2]??"";
  if(!action&&method==="PATCH"){
    const update=await req.json();
    if(typeof update.enabled==="boolean"){if(update.enabled&&!t.enabled)t.active_since=stamp();t.enabled=Number(update.enabled);}
    if(typeof update.title==="string"&&update.title.trim())t.title=update.title.trim().slice(0,160);
    if("category" in update)t.category=String(update.category??"").trim().slice(0,40)||null;
    await put(db,"targets",t).run();return json(t);
  }
  if(!action&&method==="DELETE"){
    t.enabled=0;t.deleted_at=stamp();
    await put(db,"targets",t).run();return json({deleted:true});
  }
  if(method==="GET"){
    if(action==="series")return json(await series(db,t,Math.max(1,Math.min(1000,Number(u.searchParams.get("limit"))||100))));
    if(action==="products")return json(await shopProducts(db,t,u.searchParams));
    if(action==="catalog.csv"){
      const latest=(await catalogs(db,t.id))[0];
      return csv([["商品ID","商品名称","商品链接"],...(latest?.items??[]).map(i=>[
        i.id,i.title,canonical(t.platform,"product",catalogId(t.platform,i))])],"catalog.csv");
    }
    if(action==="series.csv"){
      const snapshots=(await rows(db,"snapshots",t.id)).sort(cmpTime);
      const keys=["observed_at","slot","source","raw_sales","exact_sales","price_fen","listing_state","hourly_delta","daily_delta","quality","want_count","daily_baseline_at","price_label"];
      return csv([keys,...snapshots.map(s=>keys.map(k=>s[k]))],"target-"+t.id+".csv");
    }
  }
  if(method==="POST"){
    if(action==="refresh")return json({id:await record(db,t,await readCurrent(t),"refresh")});
    if(action==="products/monitoring")return json(await changeMonitoring(db,t,await req.json()));
    if(action==="review-sampling"){
      t.reviewed_sampling_slot=(await req.json()).slot;
      await put(db,"targets",t).run();return json({reviewed:true});
    }
    if(action==="readings"||action==="readings/visible-text"){
      let reading=await req.json();
      if(action==="readings/visible-text"){
        if(t.kind!=="product"||t.platform!=="xianyu")fail("仅支持闲鱼商品详情文字");
        const input=String(reading.text||"").split(/为你推荐|猜你喜欢|相关推荐/)[0];
        const count=input.match(/(\d+)\s*人\s*想要/), price=input.match(/[¥￥]\s*(\d+(?:\.\d{1,2})?)/);
        reading={want_count:count?Number(count[1]):null,price_fen:price?xhsPrice(price[1]):null};
      }
      if(reading.raw_sales)reading={...reading,...salesMetric(reading.raw_sales)};
      if(!["price_fen","want_count","raw_sales","exact_sales","listing_state"].some(k=>reading[k]!=null))fail("请填写有效观测值");
      for(const k of ["price_fen","want_count","exact_sales"])
        if(reading[k]!=null&&(!Number.isSafeInteger(reading[k])||reading[k]<0))fail("观测数值无效");
      const id=await record(db,t,reading,"manual");
      return json({id,price_fen:reading.price_fen,want_count:reading.want_count},201);
    }
  }
  fail("接口不存在",404);
}
export default {
  async fetch(req,env){
    try{return (await authGate(req,env)) ?? await handle(req,env);}
    catch(error){
      if(!error.status) console.error(String(error.message));
      return json({detail:error.status?error.message:"暂时无法完成请求"},error.status||500);
    }
  },
};

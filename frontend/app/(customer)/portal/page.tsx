"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CustomerShell, useCustomer } from "../../customer";

type Subscription = { id: number; subscription_no: string; status: string; traffic_quota_bytes: number; traffic_used_bytes: number; service_expire_at: string };
function PortalHome() { const { request } = useCustomer(); const [subscriptions, setSubscriptions] = useState<Subscription[]>([]); const [error, setError] = useState(""); useEffect(() => { request("/subscriptions").then(async (r) => { if (!r.ok) { setError("订阅加载失败"); return; } setSubscriptions(await r.json() as Subscription[]); }).catch(() => setError("订阅加载失败")); }, [request]); const active = subscriptions[0]; const percent = active?.traffic_quota_bytes ? Math.min(100, active.traffic_used_bytes / active.traffic_quota_bytes * 100) : 0; return <>{error && <p className="notice error">{error}</p>}{active ? <><section className="usage-alert">当前已使用流量达 {percent.toFixed(1)}%</section><section className="subscription-card"><div><p className="eyebrow">当前订阅</p><h2>{active.subscription_no}</h2><p>{active.status} · 到期：{new Date(active.service_expire_at).toLocaleString("zh-CN")}</p></div><div><strong>{(active.traffic_used_bytes / (1024 ** 3)).toFixed(2)} / {(active.traffic_quota_bytes / (1024 ** 3)).toFixed(2)} GB</strong><div className="progress"><span style={{ width: `${percent}%` }} /></div><Link href={`/subscriptions/${active.id}`}>查看订阅详情</Link></div></section></> : <section className="panel"><h2>还没有订阅</h2><p>选择一个套餐开始使用。</p><Link className="button-link" href="/plans">查看套餐</Link></section>}</>; }
export default function Portal() { return <CustomerShell title="我的服务"><PortalHome /></CustomerShell>; }

"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CustomerShell, useCustomer } from "../../customer";

type Plan = { id: number; plan_code: string; name: string; traffic_limit_bytes: number; duration_days: number; price: string; currency: string };
// TASK-S06: GET /plans now returns only the catalogue's sellable tiers, so
// this page no longer carries its own copy of the plan-code list.
function gb(bytes: number) { return `${bytes / (1024 ** 3)} GB`; }
function Plans() { const { request } = useCustomer(); const [plans, setPlans] = useState<Plan[]>([]); const [error, setError] = useState(""); useEffect(() => { request("/plans").then(async (r) => { if (!r.ok) { setError("套餐加载失败"); return; } setPlans(await r.json() as Plan[]); }).catch(() => setError("套餐加载失败")); }, [request]); return <>{error && <p className="notice error">{error}</p>}<div className="plan-grid">{plans.map((plan) => <article className="plan-card" key={plan.id}><p className="eyebrow">{plan.plan_code}</p><h2>{plan.name}</h2><strong>{gb(plan.traffic_limit_bytes)}</strong><p>有效期 {plan.duration_days} 天</p><p className="plan-price">¥{plan.price}</p><Link className="button-link" href={`/orders/new?plan_id=${plan.id}`}>立即下单</Link></article>)}</div></>; }
export default function PlansPage() { return <CustomerShell title="套餐列表"><Plans /></CustomerShell>; }

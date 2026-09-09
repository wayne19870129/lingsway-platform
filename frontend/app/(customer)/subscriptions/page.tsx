"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { CustomerShell, useCustomer } from "../../customer";

type Subscription = { id: number; subscription_no: string; status: string; traffic_quota_bytes: number; traffic_used_bytes: number; service_expire_at: string };
function Subscriptions() { const { request } = useCustomer(); const [rows, setRows] = useState<Subscription[]>([]); const [error, setError] = useState(""); useEffect(() => { request("/subscriptions").then(async (r) => { if (!r.ok) { setError("订阅加载失败"); return; } setRows(await r.json() as Subscription[]); }); }, [request]); return <>{error && <p className="notice error">{error}</p>}<section className="table-panel"><div className="table-scroll"><table><thead><tr><th>订阅</th><th>状态</th><th>用量</th><th>到期时间</th><th>操作</th></tr></thead><tbody>{rows.map((row) => <tr key={row.id}><td><strong>{row.subscription_no}</strong></td><td>{row.status}</td><td>{(row.traffic_used_bytes / (1024 ** 3)).toFixed(2)} / {(row.traffic_quota_bytes / (1024 ** 3)).toFixed(2)} GB</td><td>{new Date(row.service_expire_at).toLocaleString("zh-CN")}</td><td><Link href={`/subscriptions/${row.id}`}>查看详情</Link></td></tr>)}</tbody></table></div>{rows.length === 0 && <p className="empty">暂无订阅。</p>}</section></>; }
export default function SubscriptionsPage() { return <CustomerShell title="我的订阅"><Subscriptions /></CustomerShell>; }

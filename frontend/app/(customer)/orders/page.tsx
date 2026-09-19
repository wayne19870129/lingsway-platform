"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { CustomerShell, useCustomer } from "../../customer";
type Order = { id: number; order_no: string; plan_id: number; amount: string; currency: string; status: string; payment_status: string; created_at: string };
function Orders() { const { request } = useCustomer(); const [orders, setOrders] = useState<Order[]>([]); const [error, setError] = useState(""); const load = useCallback(() => request("/orders").then(async (r) => { if (!r.ok) { setError("订单加载失败"); return; } setOrders(await r.json() as Order[]); }), [request]); useEffect(() => { void load(); }, [load]); return <><div className="toolbar"><span>你的订单和付款状态</span><Link className="button-link" href="/orders/new">新建订单</Link></div>{error && <p className="notice error">{error}</p>}<section className="table-panel"><div className="table-scroll"><table><thead><tr><th>订单号</th><th>金额</th><th>订单状态</th><th>付款状态</th><th>时间</th></tr></thead><tbody>{orders.map((o) => <tr key={o.id}><td><strong>{o.order_no}</strong></td><td>¥{o.amount}</td><td>{o.status}</td><td>{o.payment_status}</td><td>{new Date(o.created_at).toLocaleString("zh-CN")}</td></tr>)}</tbody></table></div>{orders.length === 0 && <p className="empty">暂无订单。</p>}</section></>; }
export default function OrdersPage() { return <CustomerShell title="订单列表"><Orders /></CustomerShell>; }

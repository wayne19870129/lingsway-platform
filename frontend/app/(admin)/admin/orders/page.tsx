"use client";

import { useCallback, useEffect, useState } from "react";
import { AdminShell, extractErrorMessage, formatBytes, formatDate, statusClass, useAdmin } from "../components";

type AdminOrder = {
  id: number;
  order_no: string;
  customer_email: string;
  plan_code: string;
  plan_name: string;
  traffic_limit_bytes: number;
  amount: string;
  currency: string;
  status: string;
  payment_status: string;
  created_at: string;
  payment_notice_at: string | null;
};

function Orders() {
  const { request } = useAdmin();
  const [orders, setOrders] = useState<AdminOrder[]>([]);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<number | null>(null);

  const load = useCallback(async () => {
    const response = await request("/admin/orders");
    if (!response.ok) { setError("订单加载失败"); return; }
    setOrders(await response.json() as AdminOrder[]);
    setError("");
  }, [request]);

  // eslint-disable-next-line react-hooks/set-state-in-effect -- 旧实现原样迁入，重构数据加载逻辑超出 T5h 迁移范围，另行处理
  useEffect(() => { void load(); }, [load]);

  async function confirm(order: AdminOrder) {
    const reference = window.prompt("请输入付款登记号", `MANUAL-${order.order_no}`);
    if (!reference?.trim()) return;
    if (!window.confirm(`确认已收到 ${order.amount} ${order.currency}，并开始开通 ${order.order_no}？`)) return;
    setBusy(order.id); setMessage(""); setError("");
    try {
      const response = await request(`/admin/orders/${order.id}/confirm-payment`, {
        method: "POST",
        body: JSON.stringify({ payment_reference: reference.trim() }),
      });
      const body = await response.json().catch(() => ({})) as { detail?: string; subscription_url?: string };
      if (!response.ok) {
        setError(extractErrorMessage(body, "确认收款或自动开通失败"));
        return;
      }
      setMessage(`订单 ${order.order_no} 已开通${body.subscription_url ? `，订阅链接：${body.subscription_url}` : ""}`);
      await load();
    } catch {
      setError("请求失败，请检查网络和后台状态");
    } finally { setBusy(null); }
  }

  return <>
    {message && <p className="notice">{message}</p>}
    {error && <p className="notice error">{error}</p>}
    <div className="toolbar"><span>显示待核款及开通失败后仍待处理的订单</span><button className="button-secondary" onClick={() => void load()}>刷新</button></div>
    <section className="table-panel">
      {orders.length === 0 ? <p className="empty">当前没有待处理订单。</p> : <div className="table-scroll"><table>
        <thead><tr><th>订单</th><th>客户</th><th>套餐</th><th>金额</th><th>状态</th><th>付款通知</th><th>下单时间</th><th>操作</th></tr></thead>
        <tbody>{orders.map((order) => <tr key={order.id}>
          <td><strong>{order.order_no}</strong><small>{order.plan_code}</small></td>
          <td>{order.customer_email}</td>
          <td>{order.plan_name}<small>{formatBytes(order.traffic_limit_bytes)}</small></td>
          <td>{order.amount} {order.currency}</td>
          <td><span className={`status ${statusClass(order.status)}`}>{order.status}</span><small>{order.payment_status}</small></td>
          <td>{order.payment_notice_at ? <span className="status status-good">已通知</span> : <span className="status status-neutral">未通知</span>}</td>
          <td>{formatDate(order.created_at)}</td>
          <td><button disabled={busy === order.id} onClick={() => void confirm(order)}>{busy === order.id ? "开通中…" : "确认收款并开通"}</button></td>
        </tr>)}</tbody>
      </table></div>}
    </section>
  </>;
}

export default function AdminOrders() {
  return <AdminShell title="待确认订单"><Orders /></AdminShell>;
}

"use client";

import { useEffect, useState } from "react";
import { AdminShell, formatDate, statusClass, useAdmin } from "./components";

type Metrics = {
  total_users: number;
  active_subscriptions: number;
  pending_orders: number;
  open_tickets: number;
  generated_at: string;
};

function Dashboard() {
  const { request } = useAdmin();
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    request("/admin/metrics").then(async (response) => {
      if (!response.ok) { setError("概览数据加载失败"); return; }
      setMetrics(await response.json() as Metrics);
    }).catch(() => setError("概览数据加载失败"));
  }, [request]);

  if (error) return <p className="notice error">{error}</p>;
  if (!metrics) return <p className="notice">正在加载概览…</p>;
  return <>
    <section className="data-summary">
      <div><span>客户总数</span><strong>{metrics.total_users}</strong></div>
      <div><span>有效订阅</span><strong>{metrics.active_subscriptions}</strong></div>
      <div><span>待处理订单</span><strong>{metrics.pending_orders}</strong></div>
      <div><span>开放工单</span><strong>{metrics.open_tickets}</strong></div>
    </section>
    <section className="panel admin-note">
      <h2>开通安全提示</h2>
      <p>确认收款会进行容量预检，并触发 Webshare、Mihomo、Xray 和订阅 Token 开通。Xray 重载可能让现有连接短暂中断。</p>
      <p className={statusClass("ok")}>最后更新：{formatDate(metrics.generated_at)} · 当前系统容量数据请查看“容量”。</p>
    </section>
  </>;
}

export default function Admin() {
  return <AdminShell title="运行概览"><Dashboard /></AdminShell>;
}

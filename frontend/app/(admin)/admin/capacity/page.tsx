"use client";

import { useEffect, useState } from "react";
import { AdminShell, statusClass, useAdmin } from "../components";

type Capacity = {
  total_quota_gb: number;
  allocated_gb: number;
  ops_reserve_gb: number;
  remaining_gb: number;
  available_ip_count: number;
  capacity_level: string;
  sellable_by_plan: Record<string, number>;
  generated_at: string;
};

function CapacityPage() {
  const { request } = useAdmin();
  const [capacity, setCapacity] = useState<Capacity | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    request("/admin/capacity").then(async (response) => {
      if (!response.ok) { setError("容量数据加载失败"); return; }
      setCapacity(await response.json() as Capacity);
    }).catch(() => setError("容量数据加载失败"));
  }, [request]);
  if (error) return <p className="notice error">{error}</p>;
  if (!capacity) return <p className="notice">正在加载容量…</p>;
  const usedPercent = capacity.total_quota_gb ? Math.min(100, ((capacity.allocated_gb + capacity.ops_reserve_gb) / capacity.total_quota_gb) * 100) : 100;
  return <>
    <section className="data-summary capacity-summary">
      <div><span>套餐总带宽</span><strong>{capacity.total_quota_gb} GB</strong></div>
      <div><span>已分配子用户</span><strong>{capacity.allocated_gb.toFixed(3)} GB</strong></div>
      <div><span>运维预留</span><strong>{capacity.ops_reserve_gb.toFixed(3)} GB</strong></div>
      <div><span>剩余可分配</span><strong>{capacity.remaining_gb.toFixed(3)} GB</strong></div>
    </section>
    <section className="panel capacity-panel">
      <div className="capacity-line"><span>配额占用</span><span>{usedPercent.toFixed(1)}%</span></div>
      <div className="progress"><span style={{ width: `${usedPercent}%` }} /></div>
      <p><span className={`status ${statusClass(capacity.capacity_level)}`}>{capacity.capacity_level}</span> · 当前可用 IP：{capacity.available_ip_count}</p>
    </section>
    <section className="table-panel capacity-sales">
      <div className="panel-heading"><h2>预计还能开通</h2><span>可用专属 IP：{capacity.available_ip_count}</span></div>
      <div className="table-scroll"><table><thead><tr><th>套餐</th><th>按带宽和 IP 预计可卖</th></tr></thead><tbody>
        {Object.entries(capacity.sellable_by_plan).map(([plan, count]) => <tr key={plan}><td>{plan}</td><td><strong>{count}</strong> 个客户</td></tr>)}
      </tbody></table></div>
      <p className="muted capacity-footnote">计算同时受剩余带宽和可用专属 IP 数量限制。每个新订单确认时仍会再次执行硬校验。</p>
    </section>
  </>;
}

export default function AdminCapacity() {
  return <AdminShell title="容量总览"><CapacityPage /></AdminShell>;
}

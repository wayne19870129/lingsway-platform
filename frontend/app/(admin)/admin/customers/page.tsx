"use client";

import { useEffect, useState } from "react";
import { AdminShell, formatBytes, formatDate, statusClass, useAdmin } from "../components";

type AdminCustomer = {
  id: number;
  customer_no: string;
  email: string;
  status: string;
  subscription_status: string | null;
  subscription_no: string | null;
  egress_ip: string | null;
  egress_code: string | null;
  used_bytes: number;
  quota_bytes: number;
  webshare_proxy_limit_gb: string | null;
  service_expire_at: string | null;
  last_usage_synced_at: string | null;
};

function Customers() {
  const { request } = useAdmin();
  const [rows, setRows] = useState<AdminCustomer[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    request("/admin/customers").then(async (response) => {
      if (!response.ok) { setError("客户数据加载失败"); return; }
      setRows(await response.json() as AdminCustomer[]);
    }).catch(() => setError("客户数据加载失败"));
  }, [request]);
  return <>
    {error && <p className="notice error">{error}</p>}
    <section className="table-panel"><div className="table-scroll"><table>
      <thead><tr><th>客户</th><th>订阅状态</th><th>出口 IP</th><th>流量</th><th>到期时间</th><th>同步时间</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.id}>
        <td><strong>{row.email}</strong><small>{row.customer_no}{row.subscription_no ? ` · ${row.subscription_no}` : ""}</small></td>
        <td><span className={`status ${statusClass(row.subscription_status)}`}>{row.subscription_status ?? "无订阅"}</span></td>
        <td>{row.egress_ip ?? "—"}<small>{row.egress_code ?? "未分配"}</small></td>
        <td>{formatBytes(row.used_bytes)} / {formatBytes(row.quota_bytes)}<small>Webshare额度：{row.webshare_proxy_limit_gb ?? "—"} GB</small></td>
        <td>{formatDate(row.service_expire_at)}</td>
        <td>{formatDate(row.last_usage_synced_at)}</td>
      </tr>)}</tbody>
    </table></div>{rows.length === 0 && <p className="empty">暂无客户记录。</p>}</section>
  </>;
}

export default function AdminCustomers() {
  return <AdminShell title="客户列表"><Customers /></AdminShell>;
}

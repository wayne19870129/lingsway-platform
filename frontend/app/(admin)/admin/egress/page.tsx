"use client";

import { useEffect, useState } from "react";
import { AdminShell, formatDate, statusClass, useAdmin } from "../components";

type EgressRow = {
  id: number;
  code: string;
  ip: string;
  port: number;
  location: string;
  isp: string;
  ip_type: string;
  status: string;
  capacity: number;
  current_count: number;
  listen_port: number;
  customer_email: string | null;
  subscription_no: string | null;
  assigned_at: string | null;
};

function Egress() {
  const { request } = useAdmin();
  const [rows, setRows] = useState<EgressRow[]>([]);
  const [error, setError] = useState("");
  useEffect(() => {
    request("/admin/egress").then(async (response) => {
      if (!response.ok) { setError("IP 池加载失败"); return; }
      setRows(await response.json() as EgressRow[]);
    }).catch(() => setError("IP 池加载失败"));
  }, [request]);
  return <>
    {error && <p className="notice error">{error}</p>}
    <section className="table-panel"><div className="table-scroll"><table>
      <thead><tr><th>出口</th><th>位置 / 运营商</th><th>类型</th><th>状态</th><th>绑定客户</th><th>监听端口</th><th>分配时间</th></tr></thead>
      <tbody>{rows.map((row) => <tr key={row.id}>
        <td><strong>{row.ip}</strong><small>{row.code} · upstream:{row.port}</small></td>
        <td>{row.location}<small>{row.isp}</small></td>
        <td>{row.ip_type}</td>
        <td><span className={`status ${statusClass(row.status)}`}>{row.status}</span><small>{row.current_count}/{row.capacity}</small></td>
        <td>{row.customer_email ?? "—"}<small>{row.subscription_no ?? "未绑定"}</small></td>
        <td>{row.listen_port}</td>
        <td>{formatDate(row.assigned_at)}</td>
      </tr>)}</tbody>
    </table></div>{rows.length === 0 && <p className="empty">暂无出口记录。</p>}</section>
  </>;
}

export default function AdminEgress() {
  return <AdminShell title="IP 池状态"><Egress /></AdminShell>;
}

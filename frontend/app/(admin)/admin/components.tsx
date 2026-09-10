"use client";

import Link from "next/link";
import {
  createContext,
  type FormEvent,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useState,
} from "react";

import { api } from "../../../lib/api";
const ADMIN_TOKEN_KEY = "lingsway_admin_access_token";

type AdminContextValue = {
  request: (path: string, init?: RequestInit) => Promise<Response>;
};

const AdminContext = createContext<AdminContextValue | null>(null);

export function useAdmin(): AdminContextValue {
  const value = useContext(AdminContext);
  if (!value) throw new Error("useAdmin must be used inside AdminShell");
  return value;
}

export function AdminShell({ title, children }: { title: string; children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- 旧实现原样迁入，重构数据加载逻辑超出 T5h 迁移范围，另行处理
    setToken(window.localStorage.getItem(ADMIN_TOKEN_KEY));
    setReady(true);
  }, []);

  const request = useCallback(async (path: string, init: RequestInit = {}) => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${token ?? ""}`);
    if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
    const response = await fetch(`${api}${path}`, { ...init, headers, cache: "no-store" });
    if (response.status === 401 || response.status === 403) {
      window.localStorage.removeItem(ADMIN_TOKEN_KEY);
      setToken(null);
    }
    return response;
  }, [token]);

  async function login(event: FormEvent) {
    event.preventDefault();
    setError("");
    const auth = await fetch(`${api}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    if (!auth.ok) {
      setError("登录失败，请检查管理员账号和密码");
      return;
    }
    const body = await auth.json() as { access_token?: string };
    if (!body.access_token) {
      setError("登录响应缺少 access_token");
      return;
    }
    const check = await fetch(`${api}/admin/metrics`, {
      headers: { Authorization: `Bearer ${body.access_token}` },
      cache: "no-store",
    });
    if (!check.ok) {
      setError("该账号没有管理员权限");
      return;
    }
    window.localStorage.setItem(ADMIN_TOKEN_KEY, body.access_token);
    setToken(body.access_token);
  }

  function logout() {
    window.localStorage.removeItem(ADMIN_TOKEN_KEY);
    setToken(null);
  }

  if (!ready) return <main><p className="notice">正在加载管理后台…</p></main>;
  if (!token) {
    return <main>
      <p className="eyebrow">ADMIN CONTROL</p>
      <h1>{title}</h1>
      <form className="panel admin-login" onSubmit={login}>
        <label>管理员邮箱<input type="email" value={email} onChange={(event) => setEmail(event.target.value)} required /></label>
        <label>密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} required /></label>
        <button type="submit">登录管理后台</button>
        {error && <p className="notice error">{error}</p>}
      </form>
    </main>;
  }

  return <AdminContext.Provider value={{ request }}>
    <main className="admin-layout">
      <aside className="admin-sidebar">
        <Link className="admin-brand" href="/admin">LINGSWAY<br /><span>CONTROL PANEL</span></Link>
        <nav className="admin-side-nav" aria-label="管理导航">
          <div className="admin-nav-group"><span>订阅</span><Link href="/admin">仪表盘</Link><Link href="/admin/egress">出口 IP</Link></div>
          <div className="admin-nav-group"><span>财务</span><Link href="/admin/orders">待确认订单</Link><Link href="/admin/capacity">容量总览</Link></div>
          <div className="admin-nav-group"><span>用户</span><Link href="/admin/customers">客户列表</Link></div>
        </nav>
        <button className="button-secondary sidebar-logout" onClick={logout}>退出登录</button>
      </aside>
      <section className="admin-content">
        <div className="admin-heading">
          <div><p className="eyebrow">ADMIN CONTROL</p><h1>{title}</h1></div>
        </div>
        {children}
      </section>
    </main>
  </AdminContext.Provider>;
}

export function formatBytes(value: number): string {
  if (!value) return "0 GB";
  return `${(value / (1024 ** 3)).toFixed(2)} GB`;
}

export function formatDate(value: string | null | undefined): string {
  return value ? new Date(value).toLocaleString("zh-CN") : "—";
}

type ValidationDetail = { msg?: string }[];

export function extractErrorMessage(body: { detail?: string | ValidationDetail }, fallback: string): string {
  const { detail } = body;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join("; ") || fallback;
  return fallback;
}

export function statusClass(value: string | null | undefined): string {
  const normalized = (value ?? "").toLowerCase();
  if (["active", "activated", "available", "ok", "paid"].includes(normalized)) return "status-good";
  if (["pending", "unpaid", "provisioning", "assigned", "watch"].includes(normalized)) return "status-warn";
  if (["failed", "provision_failed", "retired", "exhausted", "low"].includes(normalized)) return "status-bad";
  return "status-neutral";
}

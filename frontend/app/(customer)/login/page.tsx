"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useState } from "react";
import { api, AuthCard, saveCustomerToken } from "../../customer";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState(""); const [password, setPassword] = useState(""); const [error, setError] = useState("");
  async function submit(event: FormEvent) { event.preventDefault(); setError(""); const response = await fetch(`${api}/auth/login`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }) }); if (!response.ok) { setError("登录失败，请检查邮箱和密码"); return; } const body = await response.json() as { access_token: string }; saveCustomerToken(body.access_token); router.push("/portal"); }
  return <AuthCard><h1>登录</h1><form className="panel form-panel" onSubmit={submit}><label>邮箱<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required /></label><label>密码<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required /></label><button type="submit">登录</button>{error && <p className="notice error">{error}</p>}</form><p>还没有账号？<Link href="/register">注册</Link></p></AuthCard>;
}

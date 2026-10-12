import React, { createContext, useContext, useEffect, useRef, useState } from "react";
import { callApi, setOperator } from "./api";

// ---------- 角色上下文（第一版本机部署，前端隐藏无权限操作，后端仍会再次校验） ----------
export const ROLES: Record<string, string> = {
  readonly: "只读",
  operator: "运维",
  responder: "应急",
};

interface AppContextValue {
  operatorId: string;
  role: string;
  setOperatorId: (id: string) => void;
  setRole: (role: string) => void;
  connected: boolean;
  lastEventAt: string;
  version: number;
}

const AppContext = createContext<AppContextValue>(null as any);
export const useApp = () => useContext(AppContext);

export const AppProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [operatorId, setOperatorId] = useState("操作员甲");
  const [role, setRole] = useState("operator");
  const [connected, setConnected] = useState(false);
  const [lastEventAt, setLastEventAt] = useState("");
  const [version, setVersion] = useState(0);
  const pollRef = useRef<number | null>(null);

  useEffect(() => setOperator(operatorId, role), [operatorId, role]);

  // SSE 状态推送；断开后每 3 秒轮询降级，不把旧数据标成实时
  useEffect(() => {
    let closed = false;
    let source: EventSource | null = null;
    const startPolling = () => {
      if (pollRef.current) return;
      const poll = async () => {
        try {
          await callApi("/v1/system/status");
          setVersion((v) => v + 1);
          setLastEventAt(new Date().toLocaleTimeString("zh-CN"));
        } catch {
          /* 断线期间忽略 */
        }
      };
      pollRef.current = window.setInterval(poll, 3000);
    };
    const stopPolling = () => {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
    const connect = () => {
      if (closed) return;
      source = new EventSource("/v1/events/stream");
      source.addEventListener("connected", () => {
        setConnected(true);
        stopPolling();
      });
      const onUpdate = () => {
        setVersion((v) => v + 1);
        setLastEventAt(new Date().toLocaleTimeString("zh-CN"));
      };
      source.addEventListener("device.updated", onUpdate);
      source.addEventListener("task.updated", onUpdate);
      source.addEventListener("alert.created", onUpdate);
      source.addEventListener("system.emergency_stopped", onUpdate);
      source.onerror = () => {
        setConnected(false);
        startPolling();
        source?.close();
        setTimeout(connect, 3000);
      };
    };
    connect();
    return () => {
      closed = true;
      source?.close();
      stopPolling();
    };
  }, []);

  useEffect(() => {
    fetch("/v1/system/status").then((r) => r.json()).catch(() => undefined);
  }, []);

  return (
    <AppContext.Provider value={{ operatorId, role, setOperatorId, setRole, connected, lastEventAt, version }}>
      {children}
    </AppContext.Provider>
  );
};

// ---------- 通用小组件 ----------
export const StatusTag: React.FC<{ display: string; kind?: "device" | "task" | "session" }> = ({ display }) => {
  const tone =
    display === "可用" || display === "已完成" || display === "已发送" || display === "有效"
      ? "tag tag-ok"
      : display === "忙碌" || display === "执行中" || display === "已排队" || display === "等待设备" || display === "校验中" || display === "正在归位" || display === "恢复中" || display === "启动中"
      ? "tag tag-busy"
      : display === "暂停" || display === "降级" || display === "维护中" || display === "即将过期" || display === "结果未知"
      ? "tag tag-warn"
      : display === "隔离" || display === "离线" || display === "明确失败" || display === "画面不可用"
      ? "tag tag-bad"
      : "tag";
  return <span className={tone}>{display}</span>;
};

export const ErrorBanner: React.FC<{ error: { title: string; suggestion: string; detail?: string } | null; onClose: () => void }> = ({ error, onClose }) => {
  if (!error) return null;
  return (
    <div className="error-banner" role="alert">
      <div>
        <strong>操作失败：{error.title}</strong>
        <div className="error-suggestion">{error.suggestion}</div>
        {error.detail && <div className="error-suggestion">{error.detail}</div>}
      </div>
      <button onClick={onClose}>关闭</button>
    </div>
  );
};

/** 危险操作需要操作原因 + 二次确认；确认后必须携带确认令牌。 */
export const ReasonDialog: React.FC<{
  title: string;
  dangerous?: boolean;
  onSubmit: (reason: string) => void;
  onCancel: () => void;
}> = ({ title, dangerous, onSubmit, onCancel }) => {
  const [reason, setReason] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  return (
    <div className="dialog-mask">
      <div className="dialog">
        <h3>{title}</h3>
        <label>
          操作原因（必填）：
          <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} placeholder="请填写本次操作的原因" />
        </label>
        {dangerous && (
          <label className="confirm-line">
            <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />
            我确认执行该危险操作（二次确认）
          </label>
        )}
        <div className="dialog-actions">
          <button onClick={onCancel}>取消</button>
          <button
            className="primary"
            disabled={!reason.trim() || (dangerous && !confirmed)}
            onClick={() => onSubmit(reason.trim())}
          >
            执行
          </button>
        </div>
      </div>
    </div>
  );
};

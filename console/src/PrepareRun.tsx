import React, { useRef, useState } from "react";
import { callApi, opContext } from "./api";
import { useApp } from "./ui";

/** Explicit operator action only: mounting or polling never prepares a run. */
export const PrepareRun: React.FC<{
  deviceId: string;
  onPrepared?: () => void | Promise<void>;
}> = ({ deviceId, onPrepared }) => {
  const { role } = useApp();
  const locked = useRef(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [failed, setFailed] = useState(false);

  const prepare = async () => {
    if (locked.current || role === "readonly") return;
    locked.current = true;
    setBusy(true);
    setMessage("");
    setFailed(false);
    try {
      const result = await callApi(`/v1/devices/${encodeURIComponent(deviceId)}/prepare-run`, {
        method: "POST",
        body: { ...opContext("已切回目标程序主页，放弃旧任务并准备新运行"), confirmed: true },
      });
      if (result.device_id !== deviceId || result.status !== "ready") {
        throw new Error(result.message || "恢复结果尚未确认，请刷新设备状态");
      }
      setMessage(result.message || "已恢复，可接收新任务；旧任务不会重跑。");
      await onPrepared?.();
    } catch (error: any) {
      setFailed(true);
      setMessage(error?.detail || error?.message || error?.title || "恢复失败，请刷新后核对");
    } finally {
      locked.current = false;
      setBusy(false);
    }
  };

  return <div className="prepare-run">
    <button onClick={prepare} disabled={busy || role === "readonly"}>
      {busy ? "正在恢复…" : "一键恢复"}
    </button>
    <p className="muted">请先切回目标程序主页并确认探头停稳；旧任务不再重跑。</p>
    {message && <p role={failed ? "alert" : "status"}>{message}</p>}
  </div>;
};

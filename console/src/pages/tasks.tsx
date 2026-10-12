import React, { useCallback, useEffect, useRef, useState } from "react";
import { callApi, opContext, currentOperator } from "../api";
import { ErrorBanner, ReasonDialog, StatusTag, useApp } from "../ui";

// ---------- 任务中心 ----------
interface TaskItem {
  task_id: string;
  task_type_display: string;
  device_id: string;
  status: string;
  status_display: string;
  created_at: string;
  finished_at?: string | null;
  uncertainty_reason?: string;
  failure_reason?: string;
  error_category?: string;
  timeline?: any[];
  evidence?: any[];
}

export const Tasks: React.FC = () => {
  const { version } = useApp();
  const [tasks, setTasks] = useState<TaskItem[]>([]);
  const [filterStatus, setFilterStatus] = useState("");
  const [filterType, setFilterType] = useState("");
  const [error, setError] = useState<any>(null);
  const [cancelTask, setCancelTask] = useState<TaskItem | null>(null);
  const [expanded, setExpanded] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (filterStatus) params.set("status", filterStatus);
      if (filterType) params.set("task_type", filterType);
      const data = await callApi(`/v1/tasks?${params.toString()}`);
      setTasks(data.tasks);
    } catch (e: any) {
      setError(e);
    }
  }, [filterStatus, filterType, version]);

  useEffect(() => {
    load();
  }, [load]);

  const doCancel = async (reason: string) => {
    if (!cancelTask) return;
    try {
      await callApi(`/v1/tasks/${cancelTask.task_id}/cancel`, { method: "POST", body: opContext(reason) });
      setCancelTask(null);
      load();
    } catch (e: any) {
      setError(e);
      setCancelTask(null);
    }
  };

  const detail = async (task: TaskItem) => {
    try {
      const full = await callApi(`/v1/tasks/${task.task_id}`);
      setExpanded(expanded === task.task_id ? null : task.task_id);
      setTasks((prev) => prev.map((t) => (t.task_id === task.task_id ? { ...t, ...full } : t)));
    } catch (e: any) {
      setError(e);
    }
  };

  return (
    <div className="page">
      <div className="page-head">
        <h2>任务中心</h2>
        <div className="head-actions">
          <select value={filterStatus} onChange={(e) => setFilterStatus(e.target.value)}>
            <option value="">全部状态</option>
            {["queued", "executing", "confirmed", "failed", "unknown", "cancelled", "expired"].map((s, i) => (
              <option key={s} value={s}>{["已排队", "执行中", "已确认完成", "明确失败", "结果未知", "已取消", "已过期"][i]}</option>
            ))}
          </select>
          <input placeholder="任务类型内部键（可选）" value={filterType} onChange={(e) => setFilterType(e.target.value)} />
        </div>
      </div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      {/* 结果未知必须置顶 */}
      {tasks.filter((t) => t.status === "unknown").length > 0 && (
        <div className="unknown-banner">有 {tasks.filter((t) => t.status === "unknown").length} 个结果未知任务，请到异常与恢复页核对。</div>
      )}
      <table className="table">
        <thead>
          <tr><th>任务编号</th><th>任务类型</th><th>设备</th><th>状态</th><th>创建时间</th><th>操作</th></tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <React.Fragment key={task.task_id}>
              <tr>
                <td>{task.task_id}</td>
                <td>{task.task_type_display}</td>
                <td>{task.device_id}</td>
                <td><StatusTag display={task.status_display} /></td>
                <td>{task.created_at}</td>
                <td>
                  <button onClick={() => detail(task)}>{expanded === task.task_id ? "收起" : "步骤时间线"}</button>
                  {["queued", "awaiting_device", "validating", "executing"].includes(task.status) && (
                    <button onClick={() => setCancelTask(task)}>取消</button>
                  )}
                </td>
              </tr>
              {expanded === task.task_id && (
                <tr>
                  <td colSpan={6}>
                    <ol className="step-line">
                      {(task.timeline ?? []).map((entry, i) => (
                        <li key={i}>{entry.at} · {entry.note || entry.status_display || ""}</li>
                      ))}
                    </ol>
                    {task.failure_reason && <div>失败原因：{task.failure_reason}（类别：{task.error_category}）</div>}
                    {task.uncertainty_reason && <div>未知原因：{task.uncertainty_reason}</div>}
                    {task.evidence?.length ? (
                      <div>证据：{task.evidence.map((e) => `${e.label}（${String(e.sha256).slice(0, 12)}…）`).join("；")}</div>
                    ) : null}
                  </td>
                </tr>
              )}
            </React.Fragment>
          ))}
        </tbody>
      </table>
      {cancelTask && (
        <ReasonDialog
          title={`取消任务：${cancelTask.task_id}（${cancelTask.task_type_display}）`}
          onSubmit={doCancel}
          onCancel={() => setCancelTask(null)}
        />
      )}
    </div>
  );
};

// ---------- 人工处理 ----------
export const Takeovers: React.FC = () => {
  const { version } = useApp();
  const [items, setItems] = useState<any[]>([]);
  const [alerts, setAlerts] = useState<any[]>([]);
  const [error, setError] = useState<any>(null);
  const [reviewTask, setReviewTask] = useState<any>(null);
  const [resolveAlert, setResolveAlert] = useState<any>(null);
  const [resumeDevice, setResumeDevice] = useState<{ device: string; mode: "quarantine" | "home" } | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await callApi("/v1/takeovers");
      setItems(data.items);
      const alertData = await callApi("/v1/alerts");
      setAlerts(alertData.alerts);
    } catch (e: any) {
      setError(e);
    }
  }, [version]);

  useEffect(() => {
    load();
  }, [load]);

  const kindLabels: Record<string, string> = {
    task_unknown: "结果未知任务",
    device_quarantined: "设备已隔离",
    home_not_confirmed: "归位未确认",
    alert_critical: "严重告警",
  };

  const submitReview = async (reason: string) => {
    if (!reviewTask) return;
    try {
      await callApi(`/v1/tasks/${reviewTask.task_id}/mark-reviewed`, { method: "POST", body: opContext(reason) });
      setReviewTask(null);
      load();
    } catch (e: any) {
      setError(e);
      setReviewTask(null);
    }
  };

  const submitResolve = async (reason: string) => {
    if (!resolveAlert) return;
    try {
      await callApi(`/v1/alerts/${resolveAlert.alert_id}/resolve`, { method: "POST", body: opContext(reason, { dangerous: false }) });
      setResolveAlert(null);
      load();
    } catch (e: any) {
      setError(e);
      setResolveAlert(null);
    }
  };

  /** 隔离设备：解除隔离（危险）→ 归位恢复检查 → 可用 */
  const recoverQuarantined = async (deviceId: string, reason: string) => {
    try {
      await callApi(`/v1/devices/${deviceId}/unquarantine`, { method: "POST", body: opContext(reason, { dangerous: true }) });
      await callApi(`/v1/devices/${deviceId}/home-check`, { method: "POST", body: opContext(`${reason}（恢复检查）`) });
      setResumeDevice(null);
      load();
    } catch (e: any) {
      setError(e);
      setResumeDevice(null);
    }
  };

  /** 归位未确认：人工确认归位（危险操作） */
  const confirmHome = async (deviceId: string, reason: string) => {
    try {
      await callApi(`/v1/devices/${deviceId}/reset-home`, { method: "POST", body: opContext(reason, { dangerous: true }) });
      setResumeDevice(null);
      load();
    } catch (e: any) {
      setError(e);
      setResumeDevice(null);
    }
  };

  return (
    <div className="page">
      <div className="page-head"><h2>异常与恢复</h2></div>
      <ErrorBanner error={error} onClose={() => setError(null)} />
      {items.length === 0 && <p className="muted">当前没有需要人工处理的条目。</p>}
      <table className="table">
        <thead><tr><th>类别</th><th>对象</th><th>原因</th><th>时间</th><th>处理</th></tr></thead>
        <tbody>
          {items.map((item, index) => (
            <tr key={index}>
              <td>{kindLabels[item.kind] ?? item.kind}</td>
              <td>{item.task_id ?? item.device_id ?? item.alert_id}</td>
              <td>{item.reason}</td>
              <td>{item.created_at}</td>
              <td>
                {item.kind === "task_unknown" && <button onClick={() => setReviewTask(item)}>标记已核对</button>}
                {item.kind === "device_quarantined" && <button onClick={() => setResumeDevice({ device: item.device_id, mode: "quarantine" })}>解除隔离并恢复检查</button>}
                {item.kind === "home_not_confirmed" && <button onClick={() => setResumeDevice({ device: item.device_id, mode: "home" })}>人工确认归位</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <section className="panel">
        <h3>告警</h3>
        <table className="table">
          <thead><tr><th>级别</th><th>内容</th><th>设备</th><th>时间</th><th>处理</th></tr></thead>
          <tbody>
            {alerts.map((alert) => (
              <tr key={alert.alert_id}>
                <td>{alert.severity_display}</td>
                <td>{alert.message}</td>
                <td>{alert.device_id ?? "—"}</td>
                <td>{alert.created_at}</td>
                <td>
                  {!alert.resolved && <button onClick={() => setResolveAlert(alert)}>处理告警</button>}
                  {alert.resolved && <span className="muted">已处理</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
      {reviewTask && (
        <ReasonDialog title={`标记结果未知任务已核对：${reviewTask.task_id}`} onSubmit={submitReview} onCancel={() => setReviewTask(null)} />
      )}
      {resolveAlert && (
        <ReasonDialog title={`处理告警：${resolveAlert.message}`} onSubmit={submitResolve} onCancel={() => setResolveAlert(null)} />
      )}
      {resumeDevice && (
        <ReasonDialog
          title={resumeDevice.mode === "quarantine" ? `解除隔离并恢复检查：${resumeDevice.device}` : `人工确认归位：${resumeDevice.device}`}
          dangerous
          onSubmit={(reason) => (resumeDevice.mode === "quarantine" ? recoverQuarantined(resumeDevice.device, reason) : confirmHome(resumeDevice.device, reason))}
          onCancel={() => setResumeDevice(null)}
        />
      )}
    </div>
  );
};
